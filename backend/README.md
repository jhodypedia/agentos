# AgentOS Backend

FastAPI backend for the AgentOS autonomous multi-agent platform.

See the repository root `README.md` for setup, `docs/ARCHITECTURE.md` for the
design and `docs/PHASES.md` for the delivery roadmap.

Quick start:

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run agentos seed
uv run uvicorn app.main:app --reload
```
