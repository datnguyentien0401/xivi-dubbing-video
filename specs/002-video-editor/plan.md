# Plan: Video & Audio Editor in the Review Dashboard

> Based on `spec.md` in the same directory. Reference `docs/architecture.md`, `docs/agent-pipeline.md`,
> and the existing "re-dub a single line" pattern at `backend/api/routers/jobs.py::update_dialogue_line` —
> this plan only summarizes the parts directly relevant to implementation, it does not repeat everything.

## Backend Changes

**DB** (`backend/api/db/models.py`, new Alembic migration):
- `jobs.timeline_edits` — new JSONB column, default `{}`, stores the current snapshot of
  tracks/clips (video, original audio, per-character dialogue tracks) as edited by the
  creator in the editor (see Clarifications #1 in `spec.md` — no separate
  `timelines`/`tracks`/`clips` tables are created). The undo/redo stack lives only in the
  client-side zustand store during the editing session, it is NOT persisted to the DB —
  only the final snapshot needs to survive a reload (US5).
- `jobs.exported_video_gcs_uri` — new column, nullable, kept separate from
  `muxed_video_gcs_uri` (the original AI version, which is never overwritten — US5).
  `ShareVideoPanel`, `GET /jobs/{id}/video-url`, `GET /jobs/{id}/share-url`,
  `POST /jobs/{id}/approve` read `exported_video_gcs_uri` if present, falling back to
  `muxed_video_gcs_uri`.

**Endpoints** (`backend/api/routers/jobs.py`):
- `GET /jobs/{id}/timeline` — returns `timeline_edits`; if empty, builds a default
  snapshot from `dialogue_lines` (which already has `start_time`/`end_time`) +
  `muxed_video_gcs_uri` as the Video track — pure DB read, no Gemini/ffmpeg calls.
- `PATCH /jobs/{id}/timeline` — debounced autosave from the frontend (every few
  seconds), overwrites `timeline_edits`. Validated via a new Pydantic model in
  `schemas/schemas.py` (`TimelineState`, `Track`, `Clip`), does not accept a raw dict.
- `POST /jobs/{id}/export` — publishes to Pub/Sub similar to `publish_job_created`
  but sets `job.current_stage = "exporting"` before publishing (a value outside the
  existing `_STAGE_ORDER` of `worker/pipeline.py`, so the worker can distinguish it
  from the single-line re-dub flow). The API only publishes, it does not run ffmpeg
  itself (Principle II).
- `PATCH /dialogue-lines/{id}` (US4 — re-dub a single line): **reuse the existing
  endpoint as-is**, nothing new added — it already follows the pattern of rewinding
  `current_stage` to `safety_checked` and re-running the pipeline, skipping lines
  that already have audio.

**Worker** (`backend/worker/`):
- `services/editor_export_service.py` (NEW, not an `Agent` — same category as
  `mux_service.py`: pure ffmpeg, no Gemini calls): reads `timeline_edits`, applies
  split/trim/delete (+ ripple) to the Video track, applies position/volume/mute/fade
  to the dialogue tracks, renders the final video at the source's original
  resolution, writes `exported_video_gcs_uri`. Reuses the `asyncio.to_thread` pattern
  (running ffmpeg synchronously off the event loop) from `mux_service.py`, but does
  **NOT** reuse `_atempo_chain` (time-stretch) as originally planned — resizing a
  clip in the editor means changing `source_start`/`source_end` (shortening/lengthening
  the source segment), not changing playback speed; atempo only makes sense for a
  different problem (forcing a dubbed take to fit exactly within the original
  dialogue line's time slot in `mux_service.py`), it does not apply to clip-resize
  operations in the editor. This decision was finalized during implementation (see
  `worker/services/editor_export_service.py`), and this doc has been updated to match
  the code.
- `main.py`: adds a branch to handle incoming messages where
  `job.current_stage == "exporting"` → calls `editor_export_service` instead of
  running `pipeline.run_pipeline` sequentially through the agents; writes export
  progress (% complete) to the Firestore doc `jobs/{job_id}` (adds an `exportProgress`
  field alongside the existing `currentStage`) — the frontend listens via the
  existing Firestore listener (`useJobStatus.ts`), NOT polling (per
  `frontend/AGENTS.md`).
- "Fit to slot" (FR-008, P2/stretch): adds a new method on the existing
  `TranslationAgent` (not a new agent — still within the transcreation/shortening
  responsibility), goes through the same Pub/Sub flow + returns its result via
  Firestore as above, with no new exception introduced for "API calls Gemini
  directly".

## Frontend Changes
- `pages/ReviewDashboard.tsx` — adds an "Open Editor" button (shown when
  `current_stage === "ready_for_review"`, even after already published — see
  Clarifications #3) leading to the editor; `ShareVideoPanel` and Approve & Publish
  switch to preferring `exported_video_gcs_uri`.
- `pages/VideoEditor.tsx` (NEW) — main layout: video preview (top) + timeline
  (bottom), following the `react-dashboard-page` skill.
- `components/editor/Timeline.tsx`, `TrackRow.tsx`, `ClipBlock.tsx`,
  `Playhead.tsx` (NEW).
- `components/editor/WaveformClip.tsx` (NEW) — uses `wavesurfer.js` (already present
  in `package.json`, not yet used anywhere) to draw the waveform on dialogue clips,
  no new dependency added.
- `stores/editorStore.ts` (NEW) — zustand store for pure UI state: selected
  track/clip, playhead, zoom, and the session's undo/redo stack (per
  `frontend/AGENTS.md` convention: zustand for state shared across many
  components, not server state).
- `hooks/useJobTimeline.ts` (NEW) — TanStack Query for `GET`/`PATCH
  /jobs/{id}/timeline`, with debouncing (a small hand-written function, no new
  library added) before calling PATCH.
- `hooks/useExportJob.ts` (NEW) — mutation calling `POST /jobs/{id}/export`;
  progress is read via the existing Firestore listener, no new polling query
  created.
- All UI copy in English (see Clarifications #4 in `spec.md`).

## Technical Risks
- The export ffmpeg filter graph (split/trim/ripple + multiple audio tracks with
  volume/fade) is significantly more complex than the current `mux_service.py`
  (which only overlays at fixed timestamps) — MVP limits cutting operations to
  "split at 1 point + delete + simple ripple" (per the MVP slice in `spec.md`),
  deferring complex multi-track fades if there isn't time.
- Cloud Run timeout when exporting video near the 30-minute limit — a risk similar
  to item 5 in `docs/architecture.md`; exporting is heavier than the original mux
  (has to re-decode many segments), consider chunking by scene if the demo uses a
  video longer than a few minutes (the demo is expected to use ≤5 minutes so the
  risk is low — see "Schedule Risk" in `spec.md`).
- `timeline_edits` doesn't have a version field from the start — if the JSON
  structure changes mid-development, older jobs may not read back correctly; add a
  `schema_version` field right in the first Pydantic model as a safeguard.
- Autosave (debounced PATCH) and the worker pipeline both write to the `jobs`
  table — different columns (`timeline_edits` vs `current_stage`) so collisions
  are unlikely, but the `UPDATE` must target that specific column (not overwrite
  the whole row) to avoid a race.

## Any architecture/tech-stack changes?
- [x] No — uses the existing stack: `wavesurfer.js` and `zustand` are already
  present in `frontend/package.json` (not yet used anywhere), `ffmpeg`/JSONB are
  already used in `mux_service.py`/`Character.voice_profile_metadata`. No new
  dependency or GCP service added.
