# AGENTS.md — XIVI

> Source of truth for all project conventions. Every AI coding agent
> (Gemini CLI, Cursor, Copilot Workspace, etc.) reads this file before generating code.
> Tool-specific files (e.g. `GEMINI.md`) should only point back here + add
> that tool's own specifics, not duplicate the content.

## 1. What this project is

**XIVI** — an agentic localization pipeline for kids' animated video, translating +
dubbing across multiple JAPAC languages, using Gemini (text/vision/native audio) as the
intelligence core.

What makes this different from a typical dubbing tool:
1. Keeping character voices consistent across an entire series (not a single generic narrator voice)
2. Contextual kid-friendly localization and humor adaptation (not literal translation)
3. Cultural safety moderation per target market + generating vocabulary learning material

Full design docs: see `docs/architecture.md`, `docs/agent-pipeline.md`,
`docs/frontend-spec.md`, `docs/backend-spec.md`, `docs/database-schema.md`,
`docs/tech-stack.md`.

## 2. Constraints — ALWAYS keep in mind when generating code

- Gemini/Gemma must be the Gen AI core, not a supporting add-on — do not train your own CV/NLP models
- Must be deployable to Cloud Run or Firebase

## 3. Mandatory architecture principles

1. **Do not train your own models.** Every intelligent task (translation, safety
   assessment, generating learning material) goes through the Gemini API (`backend/worker/services/gemini_client.py`).
2. **One responsibility per agent, unified interface** — `Agent.run(context) -> context`
   (see `backend/worker/agents/base.py`). Adding a new feature → ask "is this a new
   agent, or does it belong to an existing one?" before modifying an existing agent.
3. **Prefer managed GCP services.** Cloud Pub/Sub instead of Kafka, Cloud Run instead of
   managing your own servers, Firestore instead of hand-rolled WebSockets. Rationale/trade-offs: `docs/tech-stack.md`.
4. **All long-running processing is asynchronous.** The API service never blocks waiting
   for an agent to finish — it returns `job_id` immediately and updates via Firestore.
5. **There is always a human review step before publishing.** No agent is allowed
   to auto-publish content for children.
6. **Do not hardcode business rules that may change** (e.g. cultural safety rules
   per country) — store them in the DB (`cultural_safety_rules`), not hardcoded in code.

## 4. General code conventions

- **Type hints/TypeScript types are mandatory** in every language — do not use `Any`/`any`
  unless truly necessary, and add a comment explaining why.
- **A Pydantic model for every input/output** in the Python backend — do not pass raw dicts
  between layers.
- **Commit message**: `<scope>: short description` with scope ∈ {`agent`, `api`, `worker`,
  `ui`, `infra`, `docs`, `specify`}. Example: `agent: add safety checker rules for VN/ID/PH`.
- **Never commit secrets** (API keys, service account JSON) — use environment variables
  + `.env` (already gitignored) or Secret Manager when deploying.
- **Test before merging**: every new agent needs at least 1 test with sample input/output
  (no need to call Gemini for real in tests — mock the response).

## 5. Subdirectories with their own AGENTS.md

- `backend/AGENTS.md` — Python-specific conventions (style, how to add a new agent, how to
  run migrations)
- `frontend/AGENTS.md` — React-specific conventions (how to add a page, component conventions)

Conventions in the child files are **additive**, not contradictory to this root file. If there
is a conflict, the root file (`AGENTS.md`) always wins.

## 6. Things NOT to do

- Do not build your own TTS/voice cloning — use Gemini Native Audio / Cloud TTS
- Do not add Kafka or a self-hosted message broker unless Pub/Sub truly cannot meet the need
- Do not let an agent auto-publish content without going through review
- Do not merge multiple agents into one large function/class — that breaks the ability to test/replace each agent independently
