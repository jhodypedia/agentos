SHELL := /bin/bash
COMPOSE := docker compose
BACKEND := backend

.DEFAULT_GOAL := help

.PHONY: help install dev test test-unit test-integration test-e2e lint format typecheck migrate migration seed admin up down logs psql redis-cli build check web-install web-check web-build backup restore scan-secrets rotate-keys prune loadtest ops-status worker bot queue deploy-preflight deploy deploy-update deploy-status docker-check

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install backend dependencies (including dev tools)
	cd $(BACKEND) && uv sync --extra dev

dev: ## Run the API with autoreload on :8000
	cd $(BACKEND) && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run all tests
	cd $(BACKEND) && uv run pytest

test-unit: ## Run tests that do not need PostgreSQL/Redis
	cd $(BACKEND) && uv run pytest -m "not integration"

test-integration: ## Run integration tests
	cd $(BACKEND) && uv run pytest -m integration

test-e2e: ## Run the end-to-end journey tests
	cd $(BACKEND) && uv run pytest tests/test_e2e.py

lint: ## Ruff lint
	cd $(BACKEND) && uv run ruff check app tests

format: ## Ruff format + autofix
	cd $(BACKEND) && uv run ruff format app tests && uv run ruff check --fix app tests

typecheck: ## Mypy
	cd $(BACKEND) && uv run mypy app

migrate: ## Apply database migrations
	cd $(BACKEND) && uv run alembic upgrade head

migration: ## Create a migration (make migration m="message")
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"

seed: ## Seed default system settings, built-in tools and built-in skills
	cd $(BACKEND) && uv run agentos seed
	cd $(BACKEND) && uv run agentos seed-skills

admin: ## Create/promote the owner account (make admin email=you@example.com)
	cd $(BACKEND) && uv run agentos create-admin --email "$(email)"

up: ## Start the full stack with Docker Compose (API, web, worker, bot)
	$(COMPOSE) up -d --build

worker: ## Run a task worker in the foreground (development)
	cd $(BACKEND) && uv run agentos worker

bot: ## Run the Telegram notification loop in the foreground (development)
	cd $(BACKEND) && uv run agentos bot

queue: ## Show the durable queue by status (make queue reap=1 to recover expired leases)
	cd $(BACKEND) && uv run agentos queue $(if $(status),--status $(status),) $(if $(reap),--reap,)

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Follow API logs
	$(COMPOSE) logs -f api

psql: ## Open psql in the Postgres container
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-agentos} -d $${POSTGRES_DB:-agentos}

build: ## Build the API and web images
	$(COMPOSE) build api web

web-install: ## Install dashboard dependencies
	cd frontend && npm ci

web-check: ## Typecheck and lint the dashboard
	cd frontend && npm run typecheck && npm run lint

web-build: ## Build the dashboard image
	$(COMPOSE) build web

sandbox-image: ## Build the sandbox image used for command execution
	$(COMPOSE) --profile sandbox build sandbox

check: lint typecheck test web-check ## Lint, typecheck, test everything

verify-migration: ## Fail if models and migrations drift
	cd $(BACKEND) && uv run alembic check

backup: ## Back up PostgreSQL, Redis and workspaces to ./backups
	scripts/backup.sh

restore: ## Restore a backup (make restore f=backups/agentos-....tar.gz)
	scripts/restore.sh "$(f)"

scan-secrets: ## Scan the repo and workspaces for leaked credentials
	cd $(BACKEND) && uv run agentos scan-secrets --path app --path tests --path ../scripts

rotate-keys: ## Re-encrypt stored provider keys under the current ENCRYPTION_KEY
	cd $(BACKEND) && uv run agentos rotate-encryption-key

prune: ## Delete rows past their retention window (make prune dry=1 to preview)
	cd $(BACKEND) && uv run agentos prune $(if $(dry),--dry-run,)

loadtest: ## Load test the read API (make loadtest email=you@example.com password=...)
	cd $(BACKEND) && uv run python ../scripts/loadtest.py --url http://localhost:$${API_PORT:-8000} $(if $(email),--email "$(email)") $(if $(password),--password "$(password)")

ops-status: ## Print runtime status and usage (make ops-status token=...)
	@curl -sS -H "Authorization: Bearer $(token)" http://localhost:$${API_PORT:-8000}/api/v1/ops/status | python3 -m json.tool

deploy-preflight: ## Check a host and .env before deploying
	scripts/deploy.sh preflight

deploy: ## Build and start the production stack (Compose + Caddy proxy)
	scripts/deploy.sh up

deploy-update: ## Rebuild and restart an existing deployment
	scripts/deploy.sh update

deploy-status: ## Health of a deployed stack
	scripts/deploy.sh status

docker-check: ## Validate the Compose files (dev and production)
	$(COMPOSE) config --quiet && $(COMPOSE) -f docker-compose.yml -f deploy/docker-compose.prod.yml config --quiet && echo "compose files valid"

research-image: ## Build the offline security research image (requires sandbox-image)
	docker build -t agentos/research:latest -f backend/sandbox/Dockerfile.research backend/sandbox
