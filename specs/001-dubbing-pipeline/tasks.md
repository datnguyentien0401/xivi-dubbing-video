# Tasks: Core Dubbing & Localization Pipeline

## Backend

- [x] `[backend]` Set up the FastAPI project skeleton + the first Alembic migration
      (all tables in `docs/database-schema.md`) — file: `backend/api/`, `backend/alembic/`
- [x] `[backend]` `gemini_client.py` — basic wrapper (text call + Redis cache) — file: `backend/worker/services/gemini_client.py`
- [x] `[backend]` `ingestion_agent.py` (depends on: gemini_client) — file: `backend/worker/agents/ingestion_agent.py`
- [x] `[backend]` `translation_agent.py` with 2 prompt modes (depends on: gemini_client, prompts.py) — file: `backend/worker/agents/translation_agent.py`
- [x] `[backend]` `voice_profiling_agent.py` (depends on: ingestion_agent finishing first) — file: `backend/worker/agents/voice_profiling_agent.py`
- [x] `[backend]` `safety_agent.py` + `cultural_safety_rules` table with sample seed data (VN/ID/PH) — file: `backend/worker/agents/safety_agent.py`
- [x] `[backend]` `dubbing_agent.py` — calls Gemini Native Audio (depends on: translation + safety finishing) — file: `backend/worker/agents/dubbing_agent.py`
- [x] `[backend]` `mux_service.py` — ffmpeg audio/video muxing — file: `backend/worker/services/mux_service.py`
- [x] `[backend]` `pipeline.py` — orchestration + checkpoint/resume — file: `backend/worker/pipeline.py`
- [x] `[backend]` Endpoints `POST /jobs`, `GET /jobs/{id}/dialogue-lines`, `PATCH /dialogue-lines/{id}`, `POST /jobs/{id}/approve` — file: `backend/api/routers/jobs.py`
- [x] `[backend]` `educational_agent.py` (can be done last, doesn't block the critical path) — file: `backend/worker/agents/educational_agent.py`

## Frontend

- [x] `[frontend]` Set up the Vite + React + Tailwind + shadcn/ui skeleton — file: `frontend/`
- [x] `[frontend]` `NewProject.tsx` — upload + select languages (can be done in parallel with backend, mock the API response first) — file: `frontend/src/pages/NewProject.tsx`
      (added: uploads video directly to GCS via signed URL, multi-select JAPAC target
      languages grouped by region; navigates after submit to `/jobs/:jobId/status` —
      Pipeline Status — per docs/frontend-spec.md section 3.2, no separate
      `/projects/:projectId` route needed)
- [x] `[frontend]` `PipelineStatus.tsx` + `PipelineStepper.tsx` — Firestore listener — file: `frontend/src/pages/PipelineStatus.tsx`
- [x] `[frontend]` `ReviewDashboard.tsx` + `AudioPlayer.tsx` + `SafetyBadge.tsx` (depends on: dialogue-lines endpoint) — file: `frontend/src/pages/ReviewDashboard.tsx`
- [x] `[frontend]` `VoiceManager.tsx` — file: `frontend/src/pages/VoiceManager.tsx`

## Infra/Docs

- [x] `[infra/docs]` Dockerfile for `api` and `worker`, trial deploy to Cloud Run — file: `backend/Dockerfile.api`, `backend/Dockerfile.worker`
      (built successfully locally; actual deployment to Cloud Run has NOT been run —
      needs a real GCP project + user confirmation, see the end-of-session report)
- [x] `[infra/docs]` Create the Pub/Sub topic + push subscription pointing to the worker — file: `infra/deploy.sh`
      (script ready, with an added step granting the run.invoker permission that was
      previously missing; has NOT actually been run against any GCP project)
- [x] `[infra/docs]` Seed `cultural_safety_rules` for at least 3 JAPAC markets
      — file: `backend/scripts/seed_cultural_safety_rules.py` (VN/ID/PH, has been
      test-run + verified idempotent against local Postgres)
- [x] `[infra/docs]` Update `docs/agent-pipeline.md` if the agent order/logic changed from the original design
