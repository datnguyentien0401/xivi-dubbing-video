# Plan: Script-First Job Creation

> Based on `spec.md` in the same directory. Reference `docs/architecture.md`,
> `docs/agent-pipeline.md`, `worker/pipeline.py` (`_STAGE_ORDER`), and the existing
> "export" feature (`specs/002-video-editor`) — attaching a video later reuses the
> exact same "transient `current_stage`, new Pub/Sub message type, worker branch
> calls one stage directly" pattern already proven there.

## Backend Changes

### DB (`backend/api/db/models.py`, new Alembic migration)

Today, `Job` only reaches its `Project` via `source_video_id → SourceVideo.project_id`
(`backend/api/routers/jobs.py:211-215`, `:553-559`, `:164-169`) — every one of those
joins breaks once a job has no source video at all. Rather than special-casing each
call site, add a direct link:

- `jobs.project_id` — **new column**, `UUID NOT NULL`, FK to `projects.id`. Migration
  adds it nullable first, backfills via
  `UPDATE jobs SET project_id = (SELECT project_id FROM source_videos WHERE source_videos.id = jobs.source_video_id)`,
  then alters to `NOT NULL` (same reasoning as the `server_default='{}'` workaround
  already documented in `alembic/versions/c01adcb2c1a8_...py:25-28` — can't add a
  `NOT NULL` column to a table with existing rows in one step). `create_jobs` and the
  new script-job endpoint both set this directly; the 3 existing join-through-
  `source_video` queries listed above are updated to read `Job.project_id` directly
  (a simplification that falls out of this change, not extra scope).
- `jobs.source_video_id` — change from `NOT NULL` to **nullable**. `NULL` means
  "script-first job, no video attached yet" — deliberately NOT a separate boolean
  column (e.g. `is_script_first`); per AGENTS.md's "don't hardcode what can be
  derived," `source_video_id IS NULL` is the single source of truth other code
  branches on.
- `dialogue_lines.segment_code` — **new column**, `String, nullable=True`. Populated
  only for lines that came from a script import; `NULL` for existing/video-first
  lines (transcription has no equivalent identifier). This is what satisfies FR-008
  ("traceable back to the script's segment identifiers") — exposed read-only on
  `DialogueLineOut`.
- No new table for the uploaded script itself — each row becomes a `DialogueLine`
  directly (per spec's Key Entities: "no new, separate data model"). No schema
  change needed on `characters` — `voice_profile_metadata` is already a free-form
  JSONB (`models.py:72`); a manual assignment is stored there as
  `{"voice_name": "<name>", "manually_assigned": true}`, read back through the
  already-existing `Character.voice_name`/`voice_id` properties
  (`models.py:76-92`) with no changes to those properties.
- `dialogue_lines.start_time`/`end_time` stay `NOT NULL Float` (no nullable-timestamp
  special case introduced) — at script-import time they're set to a sequential
  placeholder (`start_time=index*1.0, end_time=(index+1)*1.0`), preserving the
  `ORDER BY start_time` ordering every downstream query already relies on
  (`jobs.py:194`, `:242`), then overwritten with real values by the new worker stage
  below before translation runs.

### Endpoints (`backend/api/routers/`)

- **`POST /projects/{project_id}/script-jobs`** (new, parallel to the existing
  `POST /jobs` at `jobs.py:68-92`) — body: script rows
  (`segment_code`, `character_name`, `text`) + `target_languages` + a
  `character_voice_assignments: dict[str, VoiceAssignment]` keyed by character name.
  Validates up front (FR-002): no duplicate `segment_code`, no empty `text`, every
  distinct `character_name` present in `character_voice_assignments`. For each
  character name: look up an existing `Character` row in the project by `name`
  first (US2's "default to a prior voice choice" — if found, reuse it and IGNORE a
  conflicting assignment in the payload rather than silently overwriting a voice
  already used elsewhere in the series); otherwise create a new `Character` with
  `voice_profile_metadata` set directly from the manual assignment, no
  `voice_sample_gcs_uri`. Creates one `Job` per target language
  (`project_id` set, `source_video_id=None`) and the full set of `DialogueLine` rows
  (character_id already resolved, `segment_code` set, placeholder timestamps per
  above). Publishes via the existing `publish_job_created`/
  `schedule_local_job_created` — **no new Pub/Sub message type for job creation
  itself**; see Worker Changes for how the pipeline picks the right first stage.
- **`GET /characters/voice-options`** (new) — returns the 12 prebuilt voice names +
  short descriptions, so the frontend can render a manual picker with zero audio
  upload. Backed by a new small public function in `gemini_client.py` (e.g.
  `list_prebuilt_voices() -> list[dict]`) wrapping the currently-private
  `_PREBUILT_VOICE_NAMES`/`_PREBUILT_VOICE_DESCRIPTIONS` (`gemini_client.py:141`) —
  the API router must not reach into a private module attribute directly.
- **`PUT /characters/{id}/voice`** (new) — sets
  `voice_profile_metadata = {"voice_name": ..., "manually_assigned": true}` directly,
  no audio sample, no Gemini call. This is genuinely new: the existing
  `AddVoiceForm`/`classify_voice_sample` flow in `VoiceManager.tsx:50-143` **always**
  requires an uploaded audio sample and a Gemini classification step — there is
  currently no "just pick a name from the list" path anywhere in the product, so
  this is not a pure reuse of what exists today.
- **`POST /jobs/{job_id}/attach-video`** (new, US5) — body: `{"source_video_id": ...}`
  (the video itself is uploaded the same way as today, via the existing
  `POST /projects/{id}/upload-url`, `projects.py:158-177` — unchanged). Validates
  `job.source_video_id is None` and `job.current_stage == "ready_for_review"`
  (FR-009's edge case: blocked before dubbing is complete). Sets
  `job.source_video_id`, moves `current_stage` to a new transient value
  `"muxing"` (exact same pattern as `"exporting"` in `jobs.py:457` /
  `specs/002-video-editor/plan.md:30-33`), publishes a new message type
  `"job_attach_video"`.
- `DialogueLineOut` gains `segment_code: str | None` (read-only, for the frontend to
  label/name exported audio files per FR-008).
- `approve_job` (`jobs.py:607-639`) currently hard-requires
  `_effective_video_gcs_uri(job)` to be non-null before publishing — this must
  branch: when `job.source_video_id is None` (script-first, no video yet), require
  instead that `current_stage == "ready_for_review"` and every non-dropped dialogue
  line has `dubbed_audio_gcs_uri` set, satisfying US4's "approve on audio alone."

### Worker (`backend/worker/`)

- **New agent**: `backend/worker/agents/script_benchmark_agent.py` (naming TBD at
  `/speckit.tasks`). Per AGENTS.md principle #2 ("one responsibility per agent") this
  is deliberately a separate agent from `IngestionAgent`, not a branch bolted onto
  it — `IngestionAgent`'s stated responsibility is "transcribe from
  `source_video_gcs_uri`" (`ingestion_agent.py:1-5`), generating a timing benchmark
  from already-known text is a different responsibility. It occupies the **same
  stage slot** (`"transcribed"`) in `_STAGE_ORDER` — no new stage name is
  introduced. Input: `context.dialogue_lines` (already has `character_id`,
  `original_text`, `segment_code`, placeholder timestamps — created by the API
  endpoint above, not by this agent). Output: real `start_time`/`end_time` per line,
  laid out cumulatively, derived from the measured duration of a generated English
  benchmark clip. Implementation reuses the **existing**
  `GeminiClient.synthesize_dubbed_audio` as-is (`target_lang="en"`, a neutral
  default voice, no `character_id`/`voice_name`/`voice_id`) — no new Gemini-client
  method needed; the WAV bytes it already returns can have their duration measured
  directly via Python's `wave` module (already imported in `gemini_client.py:14` for
  `_pcm_to_wav`), then discarded (never stored, never shown to the user). On resume
  after a crash, skips any line whose `start_time`/`end_time` are no longer the
  sequential placeholder (mirrors `DubbingAgent`'s existing
  "skip if `dubbed_audio_gcs_uri` already set" pattern, `dubbing_agent.py:86-89`) —
  avoids burning an extra Gemini TTS call twice. A line whose benchmark generation
  fails is skipped with a warning (FR-005), reusing the exact
  "skip 1 line, log a warning, don't crash the job" pattern already established in
  `ingestion_agent.py` for malformed transcript lines.
- **`worker/pipeline.py`** (`_STAGE_ORDER` unchanged, `:27-34`):
  - `Pipeline.__init__` gains a new constructor arg, `script_benchmark: ScriptBenchmarkAgent`.
  - The stage-dispatch loop currently runs a fixed `self._agents` list built once in
    `__init__` (`:58-64`). The `"transcribed"` stage now picks its agent at RUN TIME:
    `self._script_benchmark if context.source_video_gcs_uri is None else ingestion`.
    This choice is always derivable from `context` alone, so it's safe across a
    mid-pipeline crash/resume (`_stage_index`, `:37-43`) — the same agent variant is
    picked again deterministically, not dependent on which process/session is
    running.
  - `stages = [*self._agents, ("muxed", None)]` (`:80`) must conditionally **omit**
    `("muxed", None)` when `context.source_video_gcs_uri is None` — the loop then
    naturally stops after `"dubbed"`, and the existing unconditional tail
    (`context.current_stage = "ready_for_review"`, `:96`) already runs regardless,
    reaching exactly the audio-only review state FR-007 asks for with no new stage
    name required.
- **`worker/main.py`**: new branch for `message_type == "job_attach_video"`
  (mirrors the existing `"job_export"` branch, `main.py:431-439`) — loads the job's
  context (now with `source_video_gcs_uri` populated), calls the **already-existing**
  `Pipeline._run_mux_stage(context)` directly (`pipeline.py:68-71`, unchanged) rather
  than the full `run()` loop, since every earlier stage is already done. Before
  muxing, probes the attached video's real duration (reusing the exact
  `_probe_duration`-style pattern already established 3 times in this codebase —
  `mux_service.py`, `gemini_client.py::_probe_local_duration_seconds`,
  `ingestion_agent.py::_probe_local_duration_seconds`) and compares it against the
  sum of the job's dialogue-line durations; if substantially different, writes a
  warning to the Firestore realtime-status doc (same mechanism `exportProgress`
  already uses for the export flow, `main.py:382` — no new DB column for a
  transient warning) per FR-010. Sets `current_stage` back to `"ready_for_review"`
  on both success and failure (same reasoning already documented for `"exporting"`,
  `jobs.py:426-433`).

## Frontend Changes

- `pages/NewScriptProject.tsx` (new) — parallel flow to `NewProject.tsx`: upload a
  CSV instead of a video file, parsed client-side into rows. Shows the distinct
  character names found, each with a voice picker (new `<VoicePicker>` component,
  backed by `GET /characters/voice-options` + `PUT /characters/{id}/voice` — cannot
  reuse `AddVoiceForm` as-is since that component's entire flow assumes an audio
  file will be uploaded, `VoiceManager.tsx:50-99`). Reuses the exact same
  `TARGET_LANGUAGE_GROUPS` list from `NewProject.tsx:18-40` (imported, not
  duplicated, to avoid the two language lists drifting apart).
- `pages/ReviewDashboard.tsx` — needs an explicit `hasVideo = !!job.source_video_id`
  branch at the top, rather than scattering null-checks through the component: when
  false, renders the per-line audio review list with no `<video>` element, and shows
  a new "Attach Video" action (upload flow reusing `POST /projects/{id}/upload-url`
  + the new `POST /jobs/{id}/attach-video`) instead of the video preview/export
  controls.
- `pages/VideoEditor.tsx` (spec 002) — its entry point must additionally guard on
  `hasVideo`, so a script-first job with no video attached never exposes "Open
  Editor" (out of scope per this spec; only relevant once a video is attached and
  the job looks like any other video-first job again).
- `hooks/useCharacters.ts` — add `useVoiceOptions()` and `useAssignVoiceManually()`
  for the two new endpoints above.
- `hooks/useDialogueLines.ts` — add `useCreateScriptJobs()` for
  `POST /projects/{id}/script-jobs`, and `useAttachVideo()` for
  `POST /jobs/{id}/attach-video`.

## Technical Risks

- **The `jobs.project_id` backfill migration touches every existing row.** Must run
  as the 3-step nullable→backfill→`NOT NULL` sequence described above; a single-step
  `NOT NULL` add would fail outright against existing data (same class of issue
  already documented for `timeline_edits`).
- **Runtime agent selection inside `Pipeline.run()`'s stage loop is a new pattern** —
  today `self._agents` is a static list decided once in `__init__`; picking between
  `ingestion`/`script_benchmark` per-context is a deviation. It's deterministic and
  resume-safe (see above), but it's the first stage whose agent isn't fixed at
  construction time, worth flagging for review rather than assuming it's a trivial
  change.
- **Extra Gemini TTS cost**: every script-first job spends one additional Native
  Audio TTS call per line (the English benchmark) beyond what a video-first job
  needs. If the TTS model's quota (a 100 requests/day limit is already noted as a
  constraint elsewhere in `gemini_client.py`) becomes tight, this doubles volume for
  script-first jobs specifically — the resume-skip behavior above limits this to
  "once per line, ever," not "once per pipeline run," but doesn't eliminate it.
- **`ReviewDashboard.tsx` conditional rendering risk**: this page is already fairly
  complex (per `specs/002-video-editor/plan.md`'s own risk notes); recommend the
  single `hasVideo` guard at the top rather than scattering `job.source_video_id`
  checks throughout, to avoid regressing the working video-first flow.
- **CSV parsing library**: check `frontend/package.json` for an existing dependency
  before adding one — a hand-rolled parser may be sufficient for the fixed 3-column
  format (`segment_code,character_name,text`) this version supports (native `.xlsx`
  is explicitly Out of Scope per `spec.md`).

## Any architecture/tech-stack changes?
- [x] No — reuses the existing Pub/Sub message-type-branch pattern (proven by the
  `"job_export"` flow from `002-video-editor`), the existing
  `synthesize_dubbed_audio` Gemini call (just pointed at `en` as a throwaway
  benchmark), and the existing JSONB `voice_profile_metadata` field. No new GCP
  service, no new frontend dependency expected beyond possibly a small CSV-parsing
  utility (see Technical Risks).
