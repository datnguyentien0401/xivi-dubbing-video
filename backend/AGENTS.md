# AGENTS.md — backend/

> Python-specific conventions. Read `../AGENTS.md` (root) first — this file only adds to it.

## Structure
- `api/` — FastAPI orchestrator service (Cloud Run service #1). Only CRUD + publishing
  to Pub/Sub, does NOT call Gemini/ffmpeg directly.
- `worker/` — agent pipeline runner (Cloud Run service #2). Receives Pub/Sub pushes,
  runs the agents in `worker/agents/` in sequence.
- `shared/` — code shared between the 2 services (mainly `prompts.py`).

## Conventions
- Every I/O function (DB, Gemini, GCS) is `async def`.
- Each agent implements the `Agent` interface in `worker/agents/base.py` — see the
  `gemini-pipeline-agent` skill when adding a new agent.
- Gemini prompts always live in `shared/prompts.py`, never hardcoded in the agent.
- Run `ruff format .` and `ruff check .` before considering a task done (the `format.sh`
  hook already runs the formatting part, but `ruff check` needs to be run manually if several files were changed).
- Migrations: `alembic revision --autogenerate -m "description"`, then review the generated file
  before running `alembic upgrade head` — never hand-edit a migration file that's already been applied.
