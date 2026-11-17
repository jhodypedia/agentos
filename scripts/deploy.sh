#!/usr/bin/env bash
# One-command deployment for a single host (VPS or bare metal).
#
#   scripts/deploy.sh preflight     check the host and .env before touching anything
#   scripts/deploy.sh up            build and start every service
#   scripts/deploy.sh update        pull-free update: rebuild, migrate, restart
#   scripts/deploy.sh status        health of every service
#   scripts/deploy.sh logs [svc]    follow logs
#
# The stack is docker-compose.yml (database, cache, API, worker, bot, web) plus
# deploy/docker-compose.prod.yml (the Caddy reverse proxy and closed host ports).
# Everything runs from the repository root; nothing is installed on the host
# except Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
PROD_ENV=".env"

die() {
	echo "error: $*" >&2
	exit 1
}

require_docker() {
	command -v docker >/dev/null 2>&1 || die "docker is not installed"
	docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
	docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable (is it running? are you in the docker group?)"
}

load_env() {
	[ -f "$PROD_ENV" ] || die "$PROD_ENV is missing. Copy .env.example to .env and fill it in."
	# shellcheck disable=SC1091
	set -a
	# shellcheck disable=SC1090
	. "$PROD_ENV"
	set +a

	# Compose cannot interpolate an environment variable into a bind-mount path,
	# so the Caddyfile is selected here and exported: the shell environment takes
	# precedence over .env for Compose. TLS_AUTO is the single switch an operator
	# sets; PROXY_CONFIG stays available as an escape hatch for a custom file.
	if [ -z "${PROXY_CONFIG:-}" ]; then
		if [ "${TLS_AUTO:-0}" = "1" ]; then
			export PROXY_CONFIG=Caddyfile.tls
		else
			export PROXY_CONFIG=Caddyfile
		fi
	fi
}

preflight() {
	require_docker
	load_env
	local problems=0

	check() {
		local name="$1" value="${2:-}" min="${3:-1}"
		if [ -z "$value" ]; then
			echo "  MISSING  $name"
			problems=$((problems + 1))
		elif [ "${#value}" -lt "$min" ]; then
			echo "  TOO SHORT $name (need at least $min characters)"
			problems=$((problems + 1))
		else
			echo "  ok       $name"
		fi
	}

	echo "Required configuration:"
	check SECRET_KEY "${SECRET_KEY:-}" 32
	check ENCRYPTION_KEY "${ENCRYPTION_KEY:-}" 43
	check POSTGRES_PASSWORD "${POSTGRES_PASSWORD:-}" 12

	if [ "${SECRET_KEY:-}" = "change-me-to-a-random-value-at-least-32-characters" ]; then
		echo "  INSECURE SECRET_KEY is still the example value"
		problems=$((problems + 1))
	fi
	if [ "${POSTGRES_PASSWORD:-}" = "change-me-strong-database-password" ]; then
		echo "  INSECURE POSTGRES_PASSWORD is still the example value"
		problems=$((problems + 1))
	fi
	if [ "${ENCRYPTION_KEY:-}" = "change-me-to-a-valid-fernet-key" ]; then
		echo "  INSECURE ENCRYPTION_KEY is still the example value"
		problems=$((problems + 1))
	fi

	if [ "${TLS_AUTO:-0}" = "1" ]; then
		if [ "${PUBLIC_HOST:-localhost}" = "localhost" ]; then
			echo "  PROBLEM  TLS_AUTO=1 needs a real PUBLIC_HOST"
			problems=$((problems + 1))
		fi
		if [ -z "${ACME_EMAIL:-}" ]; then
			echo "  PROBLEM  TLS_AUTO=1 needs ACME_EMAIL for expiry notices"
			problems=$((problems + 1))
		fi
		echo "  ok       TLS_AUTO=1 (Caddy will obtain a certificate for ${PUBLIC_HOST:-localhost})"
	fi

	echo
	echo "Host:"
	if [ -r /etc/os-release ]; then
		# shellcheck disable=SC1091
		. /etc/os-release
		echo "  ${PRETTY_NAME:-unknown}"
	fi
	echo "  docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
	local available_kb
	available_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
	echo "  free disk: $((available_kb / 1024 / 1024)) GiB"
	if [ "$available_kb" -lt 5242880 ]; then
		echo "  WARNING  less than 5 GiB free; images and the database need room"
	fi

	echo
	if [ "$problems" -gt 0 ]; then
		die "$problems configuration problem(s). Fix them before deploying."
	fi
	echo "Preflight passed."
}

up() {
	require_docker
	load_env
	# Build the web image with an empty API URL so the dashboard calls the API on
	# its own origin through the proxy. Passing an absolute URL here would bake
	# the host into the bundle and force CORS configuration.
	NEXT_PUBLIC_API_URL="" "${COMPOSE[@]}" up -d --build
	wait_healthy
	echo
	if [ "${TLS_AUTO:-0}" = "1" ]; then
		echo "Deployed. Open https://${PUBLIC_HOST}"
	else
		echo "Deployed. Open http://${PUBLIC_HOST:-localhost}:${PROXY_PORT:-80}"
	fi
	echo "First run? Create the owner account: make admin email=you@example.com"
}

update() {
	require_docker
	load_env
	# The API's entrypoint applies migrations before it starts, so a rebuild plus
	# restart is enough. Services are recreated one at a time rather than all at
	# once, which keeps the old API answering while the new one boots.
	NEXT_PUBLIC_API_URL="" "${COMPOSE[@]}" up -d --build api
	"${COMPOSE[@]}" up -d --build worker web bot proxy
	wait_healthy
	echo "Updated."
}

wait_healthy() {
	echo "Waiting for services to become healthy..."
	local deadline=$((SECONDS + 180))
	while [ "$SECONDS" -lt "$deadline" ]; do
		local states
		states="$("${COMPOSE[@]}" ps --format '{{.Service}}={{.State}}' 2>/dev/null || true)"
		local unhealthy
		unhealthy="$(echo "$states" | grep -vE '=(running|exited)' || true)"
		if [ -z "$unhealthy" ] && "${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' | grep -q 'healthy'; then
			echo "All services are up."
			"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
			return 0
		fi
		sleep 5
	done
	"${COMPOSE[@]}" ps
	die "services did not become healthy in time; check: scripts/deploy.sh logs"
}

status() {
	require_docker
	load_env
	"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
	echo
	# Probe through the proxy when it is running, because that is the path a user
	# takes; fall back to the API's own port when the proxy is not part of the
	# running stack (the plain `make up` development stack).
	local base="http://127.0.0.1:${PROXY_PORT:-80}"
	if ! "${COMPOSE[@]}" ps --services --filter status=running | grep -qx proxy; then
		base="http://127.0.0.1:${API_PORT:-8000}"
		echo "(proxy not running; checking the API directly on ${base})"
	fi
	echo -n "API health:  "
	curl -fsS -m 5 "$base/health/ready" >/dev/null && echo "ok" || echo "FAILED"
	echo -n "Dashboard:   "
	if [ "$base" = "http://127.0.0.1:${PROXY_PORT:-80}" ]; then
		curl -fsS -m 5 -o /dev/null "$base/login" && echo "ok" || echo "FAILED"
	else
		curl -fsS -m 5 -o /dev/null "http://127.0.0.1:${WEB_PORT:-3000}/login" && echo "ok" || echo "FAILED"
	fi
	echo -n "Worker:      "
	"${COMPOSE[@]}" exec -T worker python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:9101/health',timeout=5); sys.exit(0)" 2>/dev/null && echo "ok" || echo "FAILED"
}

logs() {
	require_docker
	"${COMPOSE[@]}" logs -f --tail=100 "${1:-}"
}

case "${1:-}" in
preflight) preflight ;;
up) up ;;
update) update ;;
status) status ;;
logs) shift || true && logs "${1:-}" ;;
*)
	echo "usage: $0 {preflight|up|update|status|logs [service]}" >&2
	exit 2
	;;
esac
