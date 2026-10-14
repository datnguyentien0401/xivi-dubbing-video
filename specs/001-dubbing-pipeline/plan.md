# Plan: Core Dubbing & Localization Pipeline

> Based on `spec.md` in the same directory. Full detailed architecture: see `docs/architecture.md`
> and `docs/agent-pipeline.md` — this plan only summarizes the parts directly relevant to
> implementation, it does not repeat everything.

## Backend Changes

**Agents** (`backend/worker/agents/`):
- `ingestion_agent.py` — Agent 1: transcript + speaker diarization
- `voice_profiling_agent.py` — Agent 2: assign a consistent character_id
- `translation_agent.py` — Agent 3: translation + transcreation (2 modes based on `is_song`)
- `safety_agent.py` — Agent 4: cultural safety review, returns `safety_status` + `reasoning`
- `dubbing_agent.py` — Agent 5: calls Gemini Native Audio to generate the audio clip
- `educational_agent.py` — Agent 7: generates vocabulary (runs in parallel, non-blocking)

**Supporting services** (`backend/worker/services/`):
- `gemini_client.py` — the single wrapper for calling Gemini, with Redis cache + retry
- `mux_service.py` — ffmpeg, muxes audio into video by timestamp
- `gcs_client.py`, `redis_client.py`

**Endpoints** (`backend/api/routers/`):
- `POST /projects/{id}/upload-url` — signed upload URL
- `POST /jobs` — create a job, publish to Pub/Sub
- `GET /jobs/{id}/dialogue-lines` — for the Review Dashboard
- `PATCH /dialogue-lines/{id}` — edit text, trigger re-dub for 1 line
- `POST /jobs/{id}/approve` — publish

**DB**: follows exactly the schema designed in `docs/database-schema.md`
(`projects`, `source_videos`, `characters`, `jobs`, `dialogue_lines`,
`vocabulary_items`, `cultural_safety_rules`) — the first Alembic migration
creates all of these tables.

## Frontend Changes
- `pages/NewProject.tsx`, `PipelineStatus.tsx`, `ReviewDashboard.tsx`,
  `VoiceManager.tsx` (per `docs/frontend-spec.md`)
- `hooks/useJobStatus.ts` — Firestore realtime listener
- `components/PipelineStepper.tsx`, `SafetyBadge.tsx`, `AudioPlayer.tsx`

## Technical Risks
- Audio/video sync after dubbing — mitigated by keeping the original timestamps
  from Agent 1, instead of concatenating sequentially (documented in
  `docs/architecture.md` section 5)
- Cloud Run timeout on long videos — MVP is limited to ≤30 minutes/video (see Out of Scope)
- Gemini Native Audio rate limits — cache by content hash in Redis

## Any architecture/tech-stack changes?
- [x] No — using exactly the stack described in `docs/tech-stack.md`
