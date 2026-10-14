# Tasks: Script-First Job Creation

> Based on `plan.md` in the same directory. Required MVP = US1 + US2 + US3 + US4 (P1);
> tasks marked **(P2)** (US5, "attach video") are done after the P1 slice works end-to-end,
> per the priorities in `spec.md`.

## Backend

- [x] `[backend]` Alembic migration: add `jobs.project_id` (nullable → backfill from
      `source_videos.project_id` via `job.source_video_id` → `NOT NULL`), change
      `jobs.source_video_id` to nullable, add `dialogue_lines.segment_code`
      (`String`, nullable) — file: `backend/api/db/models.py`,
      `backend/alembic/versions/8ecf7d328f3a_*.py`. Applied and verified against
      the real local Postgres (`\d jobs`/`\d dialogue_lines` confirmed the exact
      expected shape); full backend suite green (210 passed, 27 skipped, only
      the 1 pre-existing unrelated failure in `test_firebase_auth.py`).
      (Also had to fix `create_jobs` (`jobs.py`), which didn't set `project_id`
      at all — every job creation would have violated the new `NOT NULL`
      constraint immediately. Updated every direct `Job(...)` test fixture
      across `test_jobs.py`, `test_project_detail.py`, `test_project_delete.py`,
      `test_ownership.py`, `worker/test_main.py` to pass `project_id` too.)
- [x] `[backend]` Update the call sites that currently reach `Project`/`Character`
      by joining through `source_video_id → SourceVideo.project_id` to read
      `Job.project_id` directly instead — turned out to be **7 call sites, not
      3** (found while actually fixing them, not caught by the plan or by the
      first code-reviewer pass — a second review round caught 2 more):
      `jobs.py::get_job_share_url` (project name for the download filename),
      `jobs.py::list_job_characters` (previously would have returned an empty
      character list for any script-first job), `jobs.py::update_dialogue_line`
      (previously would have raised `NoResultFound`),
      `projects.py::delete_project` (previously would have left a script-first
      job's row undiscovered, hitting a FK violation against the project row
      being deleted), `projects.py::list_project_jobs` (previously would have
      silently excluded script-first jobs from the Project Detail page),
      `ownership.py::get_owned_job` and `ownership.py::get_owned_dialogue_line`
      (previously would have 404'd a script-first job's own legitimate owner on
      nearly every job-scoped endpoint — these 2 gate almost all of them, the
      most load-bearing miss of the 7). Also fixed, same bug class:
      `worker/main.py::_load_context` resolved `project` via
      `source_video.project_id`, which would `AttributeError` the moment
      `source_video` is `None` — now reads `job.project_id` directly. And
      widened `JobOut.source_video_id` from `UUID` to `UUID | None` (schemas.py)
      to match the now-nullable column — Pydantic would otherwise raise a
      `ValidationError` serializing any script-first job. Files:
      `backend/api/routers/jobs.py`, `backend/api/routers/projects.py`,
      `backend/api/core/ownership.py`, `backend/worker/main.py`,
      `backend/api/schemas/schemas.py`. Depends on: the migration above.
      **Risk flagged for the `ScriptBenchmarkAgent`/`pipeline.py` task below**:
      `worker/main.py::_load_context` still does
      `source_video = await session.get(SourceVideo, job.source_video_id)` then
      later reads `source_video.gcs_uri` for `PipelineContext.source_video_gcs_uri`
      (currently a required, non-optional `str` field in
      `worker/models/pipeline_context.py`) — this WILL crash for a script-first
      job (`source_video` is `None`) until that task makes
      `source_video_gcs_uri` optional and teaches `_load_context` to skip
      reading `.gcs_uri` when there's no video. Not fixed here — doing so
      requires the same context-loading redesign that task already owns.
- [x] `[backend]` Pydantic schemas: `ScriptLineImport` (`segment_code`,
      `character_name`, `text`), `VoiceAssignment`, `ScriptJobCreate`
      (script rows + `target_languages` + `character_voice_assignments: dict[str,
      VoiceAssignment]`, with a `model_validator` rejecting duplicate
      `segment_code` and any character missing a voice assignment), and
      `segment_code: str | None = None` added to `DialogueLineOut` — file:
      `backend/api/schemas/schemas.py`, `backend/api/schemas/test_schemas.py`
      (new, 9 tests). code-reviewer found 1 real gap: `Field(min_length=1)`
      alone accepts a whitespace-only string (pydantic doesn't strip before
      counting length) — a `text="   "` row would've silently become a
      dialogue line with blank-but-"present" content. Fixed with a
      `field_validator` that strips and rejects blank-after-strip on
      `segment_code`/`character_name`/`text`/`VoiceAssignment.voice_name`,
      plus regression tests. Full suite green (225 passed, 27 skipped, only
      the 1 pre-existing unrelated failure).
- [x] `[backend]` `POST /projects/{project_id}/script-jobs` — validates no
      duplicate `segment_code`, no empty `text`, every distinct `character_name`
      covered by `character_voice_assignments`; resolves each character name
      against an existing `Character` row in the project first (reuse its voice),
      otherwise creates one from the manual assignment; creates one `Job` per
      target language (`project_id` set, `source_video_id=None`) and the full set
      of `DialogueLine` rows (`segment_code` set, placeholder sequential
      timestamps); publishes via the existing `publish_job_created`/
      `schedule_local_job_created` (no new Pub/Sub message type) — file:
      `backend/api/routers/projects.py` (not `jobs.py` as originally estimated
      — the path `/projects/{project_id}/script-jobs` matches this router's
      existing `/projects` prefix, same reasoning `list_project_jobs` in the
      same file already follows), `backend/api/routers/test_projects.py` (5
      new tests). Depends on: migration task, schemas task.
      code-reviewer found 1 real gap: silently ignoring a conflicting voice
      assignment for an already-existing character (US2) had no log line,
      inconsistent with this codebase's established "always log when
      silently altering/dropping data" convention (see
      `ingestion_agent.py`'s duration backstop). Fixed with a
      `logger.warning` + regression test. Also confirmed (not a blocker, just
      documented): no unique constraint on `characters(project_id, name)`
      means 2 concurrent requests creating the same new character name could
      race — pre-existing risk class already present in
      `create_character`/`characters.py`, not a new regression from this
      endpoint. Full suite green (229 passed, 27 skipped, only the 1
      pre-existing unrelated failure).
- [x] `[backend]` `GET /characters/voice-options` (wraps a new public
      `list_prebuilt_voices()` in `gemini_client.py` around the existing private
      `_PREBUILT_VOICE_NAMES`/`_PREBUILT_VOICE_DESCRIPTIONS`) + `PUT
      /characters/{id}/voice` (sets `voice_profile_metadata =
      {"voice_name": ..., "manually_assigned": true}` directly, no audio, no
      Gemini call) — file: `backend/api/routers/characters.py`,
      `backend/api/routers/test_characters.py` (5 new tests),
      `backend/worker/services/gemini_client.py`,
      `backend/worker/services/test_gemini_client.py` (1 new test). Done in
      parallel ahead of the script-jobs endpoint, as planned.
      `list_prebuilt_voices()` returns `list[VoiceOption]` (a real Pydantic
      model, imported from `api.schemas.schemas` — precedent for worker/
      importing api/schemas already exists in `worker/main.py` and
      `editor_export_service.py`), not a raw `list[dict]` — code-reviewer
      caught the initial raw-dict version as an AGENTS.md §4 violation before
      this was marked done. Full suite green (225 passed, 27 skipped, only
      the 1 pre-existing unrelated failure).
- [x] `[backend]` Branch `approve_job` (`jobs.py:608-639`) on
      `job.source_video_id is None`: for an audio-only job, require
      `current_stage == "ready_for_review"` and every non-dropped dialogue line
      to have `dubbed_audio_gcs_uri` set, instead of requiring
      `_effective_video_gcs_uri(job)` — file: `backend/api/routers/jobs.py`,
      `backend/api/routers/test_jobs.py` (5 new tests). Depends on: migration
      task (nullable `source_video_id`).
      code-reviewer notes (no code change required, both addressed below):
      (a) "non-dropped" is interpreted as `safety_status == "ok"` — a line
      the reviewer deliberately leaves flagged (DubbingAgent never dubs it by
      design) does NOT block approval, only an "ok" line still lacking audio
      does; also requires at least 1 line to actually have dubbed audio
      (rejects approving a job with zero dubbed content, analogous to the
      video-first branch's "no result video yet" guard). (b) 2 real gaps
      surfaced, NOT part of this task's own scope, tracked as new items
      below so they aren't lost: `ReviewDashboard.tsx` currently disables
      Approve whenever ANY line is flagged, for every job type — this
      directly conflicts with the backend now correctly allowing approval of
      an audio-only job with an intentionally-flagged line, making that path
      unreachable from the UI until the `hasVideo` frontend task below
      accounts for it; and FR-008's "downloadable set of dubbed audio files"
      deliverable has no endpoint anywhere yet — added as a new task below.
      Full suite green (234 passed, 27 skipped, only the 1 pre-existing
      unrelated failure).
- [x] `[backend]` `GET /jobs/{job_id}/dubbed-audio-bundle` (new — found missing
      during code review of the `approve_job` task above; FR-008's "named,
      downloadable set of dubbed audio files, traceable back to the script's
      segment identifiers" has no endpoint anywhere yet). Zips every dialogue
      line's `dubbed_audio_gcs_uri` (downloaded via `StorageClient`), named by
      `segment_code` (falls back to `line-{index+1:03d}` for a video-first
      job's lines, with defensive de-dup if two names collide), uploads the
      zip, returns a signed URL (same `SignedReadUrlResponse` pattern as
      `get_dialogue_line_audio_url`) — file: `backend/api/routers/jobs.py`,
      `backend/api/routers/test_jobs.py` (3 tests: segment_code naming,
      index fallback, 404 when nothing is dubbed yet).
      Depends on: `approve_job` audio-only branch task (same file/area),
      `ScriptBenchmarkAgent`+`pipeline.py` tasks below (nothing to zip until a
      script-first job can actually reach `dubbed`).
      Manually verified live via `curl` against a real script-first job's
      bundle URL — downloaded zip's `unzip -l` showed exactly
      `001.wav`/`002.wav`/`003.wav` matching the job's real segment codes.
      `code-reviewer` found one real bug: the `zipfile.ZipFile(...).write(...)`
      calls (blocking disk I/O + CPU-bound DEFLATE) ran directly inside the
      `async def` endpoint with no `asyncio.to_thread` wrapper — unlike every
      other blocking call in this file (`download_to_path`,
      `upload_from_path`, `generate_signed_read_url`, all wrapped inside
      `gcs_client.py`/`local_storage_client.py`). Since `xivi-api` runs
      a single-process Uvicorn with `concurrency: 80` (no `--workers` flag,
      `docs/architecture.md`), this would stall the entire event loop —
      freezing every other concurrent request on the instance — for as long
      as the zip took to build, directly contradicting the endpoint's own
      docstring claim of being "lightweight, comparable to signed-URL
      generation." Fixed by splitting the per-line download loop (already
      `async`, left as-is) from the zip-writing step, and moving the latter
      into a new sync helper `_write_zip()` called via
      `await asyncio.to_thread(_write_zip, ...)`. Re-ran all 3 endpoint tests
      (pass) and the full backend suite (286 passed, 3 pre-existing
      unrelated `ffmpeg`-environment failures in `test_mux_service.py` — a
      local-host ffmpeg/codec mismatch outside the Docker worker image, not
      caused by this change; not reproducible inside the worker container).
- [x] `[backend]` New agent `ScriptBenchmarkAgent` — occupies the `"transcribed"`
      stage slot; for each dialogue line still at its placeholder timestamp,
      calls the existing `GeminiClient.synthesize_dubbed_audio` with
      `target_lang="en"` to generate a throwaway benchmark clip, measures its
      duration via `wave` (discarding the audio itself), lays out real
      `start_time`/`end_time` cumulatively; skips (with a warning) a line whose
      benchmark generation fails, mirroring `ingestion_agent.py`'s existing
      "skip 1 bad line" pattern — file:
      `backend/worker/agents/script_benchmark_agent.py`,
      `backend/worker/agents/test_script_benchmark_agent.py` (6 tests).
      Depends on: migration task (`segment_code`, placeholder timestamps
      already on the rows), schemas task.
      Deliberately dropped the per-line "already benchmarked, skip it"
      idempotency plan.md originally described — matches
      `translation_agent.py`/`safety_agent.py`'s own existing "reprocess
      every line from scratch on retry" trade-off instead of a fragile
      value-based placeholder check; code-reviewer independently traced the
      checkpoint-write path and confirmed a crash mid-run can only ever cost
      extra Gemini quota, never corrupt the cumulative timestamp cursor.
      gemini-prompt-reviewer found 1 real, launch-blocking gap: the cache
      key in `gemini_client.py::_cache_key` doesn't include `character_id`
      and uses the pre-resolution `voice_name` — without a distinct id, a
      same-language/same-text/same-fallback-voice real `DubbingAgent` call
      for the SAME line could cache-hit this throwaway benchmark audio and
      silently ship it as the final dub (wrong voice, pre-translation
      text). Fixed by prefixing `dialogue_line_id="benchmark:{line.id}"` so
      the two call sites can never share a cache key, plus a regression
      test.
- [x] `[backend]` `worker/pipeline.py`: `Pipeline.__init__` gains a
      `script_benchmark: ScriptBenchmarkAgent` argument; the `"transcribed"`
      stage picks `script_benchmark` vs. `ingestion` at run time based on
      `context.source_video_gcs_uri is None` (deterministic, resume-safe); the
      `("muxed", None)` tail stage is omitted from `stages` when
      `context.source_video_gcs_uri is None`, so the loop stops after
      `"dubbed"` and falls through to the existing unconditional
      `current_stage = "ready_for_review"` — file: `backend/worker/pipeline.py`,
      `backend/worker/test_pipeline.py` (new, 4 tests — no dedicated test
      file existed for `Pipeline` before this). Depends on:
      `ScriptBenchmarkAgent` task.
      Also required (flagged as a known risk in an earlier task's done-note):
      `worker/models/pipeline_context.py::PipelineContext.source_video_gcs_uri`
      widened from `str` to `str | None`; `worker/main.py::_load_context`
      updated to pass `None` instead of crashing on a `None` `SourceVideo`.
      Verified (added a regression test, no code change needed)
      `VoiceProfilingAgent` is already a safe no-op for a script-first job —
      its video-download branch only triggers when a line has a
      `speaker_tag`, which script-first lines never get.
      code-reviewer independently traced all 6 `_stage_index`/job-type
      combinations and confirmed the existing (unchanged) resume logic is
      correct, then found 1 more real gap this change newly makes
      reachable: `POST /jobs/{id}/export` had no guard against a
      script-first job with no video — once such a job can reach
      `ready_for_review` (previously impossible), calling export would crash
      `worker/main.py::_export_job` with `AttributeError` on a `None`
      `SourceVideo`. Fixed with the same `source_video_id is None` 409 guard
      pattern as `approve_job`'s audio-only branch, plus a regression test.
      `docs/agent-pipeline.md` still needs updating for this change — already
      tracked as its own separate Infra/Docs task below, not done yet.
      Full suite green (246 passed, 27 skipped, only the 1 pre-existing
      unrelated failure).
- [x] `[backend]` `POST /jobs/{job_id}/attach-video` **(P2)** — body:
      `{"source_video_id": ...}` (video itself uploaded via the existing
      `POST /projects/{id}/upload-url`, unchanged); validates
      `job.source_video_id is None` and `job.current_stage ==
      "ready_for_review"`; sets `job.source_video_id`, moves `current_stage` to
      a new transient `"muxing"` value, publishes a new message type
      `"job_attach_video"` — file: `backend/api/routers/jobs.py` (not
      `jobs.py`'s original estimate changed — it IS `jobs.py`, confirmed
      correct), `backend/api/schemas/schemas.py` (`AttachVideoRequest`),
      `backend/api/core/pubsub_client.py`/`local_pubsub_client.py`. Depends
      on: migration task, the `approve_job` branching task (same
      file/section).
      Added 1 guard beyond the original description, found necessary while
      implementing: `get_owned_source_video` only confirms the CALLING USER
      owns the video, not that it belongs to the SAME PROJECT as the job —
      added an explicit `source_video.project_id != job.project_id` 404
      check (same precedent as `update_dialogue_line`'s character/project
      mismatch check), otherwise a user could attach a video from one of
      their OTHER projects.
- [x] `[backend]` `worker/main.py`: new branch for `message_type ==
      "job_attach_video"` (mirrors the existing `"job_export"` branch) — loads
      the job's context (now with `source_video_gcs_uri` populated), calls the
      existing `Pipeline._run_mux_stage(context)` directly; before muxing,
      probes the attached video's real duration and compares it against the sum
      of the job's dialogue-line durations, writing a warning to the Firestore
      realtime-status doc (same mechanism as `exportProgress`) if substantially
      different; sets `current_stage` back to `"ready_for_review"` on both
      success and failure **(P2)** — file: `backend/worker/main.py`. Depends on:
      the `pipeline.py` task, the `attach-video` endpoint task.
      code-reviewer found 1 real bug before this was marked done: the
      "expected duration" comparison summed `end_time` over ALL dialogue
      lines, including a permanently flagged/undubbed line (allowed to
      exist forever on a published audio-only job, see `approve_job`'s
      audio-only branch) — if such a line was chronologically last, it
      inflated the proxy past what the real muxed audio ever covers,
      producing a false-positive mismatch warning with a factually wrong
      message. Fixed by filtering to only lines with `dubbed_audio_gcs_uri`
      set (matching `mux_for_job`'s own filter), plus a regression test.
      Also added a test asserting `Pipeline.run` (the full stage loop) is
      never called — the task's core guarantee (translation/safety/dubbing
      never re-run) was previously only inferred from `_run_mux_stage` being
      called, not actually proven.
      Full suite green (257 passed, 27 skipped, only the 1 pre-existing
      unrelated failure).
- [x] `[backend]` Endpoint tests for `script-jobs` creation (duplicate
      segment_code, missing character assignment, happy path), `voice-options`/
      `PUT voice`, `approve_job`'s audio-only branch, and `attach-video`
      **(attach-video tests are P2)** — file: `backend/api/routers/test_jobs.py`,
      `backend/api/routers/test_characters.py`. Depends on: the respective
      endpoint tasks above.
      Done incrementally alongside each endpoint's own task above rather
      than as one separate pass at the end — every scenario listed here is
      covered (confirmed by re-reading each task's done-note above), so
      this is a bookkeeping checkbox, not separate remaining work.

## Frontend

- [x] `[frontend]` `<VoicePicker>` component + `useVoiceOptions()`/
      `useAssignVoiceManually()` hooks — file:
      `frontend/src/components/VoicePicker.tsx`,
      `frontend/src/hooks/useCharacters.ts`. Wired directly to the real
      endpoints (backend was already done). `VoicePicker` is a plain
      controlled selector (`value`/`onChange`), not coupled to a specific
      `character_id` — reusable both for a character that doesn't exist in
      the DB yet (script-first job creation) and for assigning a voice to an
      already-existing one (via `useAssignVoiceManually`, not yet wired up
      anywhere else — available for `VoiceManager.tsx`/`ReviewDashboard.tsx`
      to use later if wanted, not required by this spec).
      Verified live in a real browser: `GET /characters/voice-options`
      returns the real 12 prebuilt voices with descriptions, rendered
      correctly in the `<select>`.
- [x] `[frontend]` `pages/NewScriptProject.tsx` — upload/parse a CSV
      (`segment_code,character_name,text`) client-side, show the distinct
      character names found each with a `<VoicePicker>`, reuse
      `TARGET_LANGUAGE_GROUPS` imported from `NewProject.tsx` + route in
      `App.tsx` — file: `frontend/src/pages/NewScriptProject.tsx`,
      `frontend/src/App.tsx`. Depends on: `<VoicePicker>` task.
      CSV parsing is hand-rolled (no new dependency, per plan.md) with
      proper quoted-field support (commas inside `"..."`, `""` as an escaped
      quote) — verified live with a quoted field ("Hello, everyone!")
      parsing correctly. Also fixed `useDialogueLines.ts`'s `Job.source_video_id`
      frontend type (`string` → `string | null`) to match the backend
      schema change from the migration task — otherwise `hasVideo` checks in
      later tasks would type-check against a field TypeScript believed was
      always truthy.
      **Verified live end-to-end in a real browser, not just unit-level**:
      created a real script-first job (2 characters, 3 lines, 1 quoted
      field) through the actual UI, confirmed via direct DB query that
      `project_id`/`source_video_id=NULL`/`segment_code`/`character_id` were
      all correct, watched the real pipeline run to completion
      (`ScriptBenchmarkAgent` → real Gemini-measured cumulative timestamps
      0→1.6→2.8→4.6s, `VoiceProfilingAgent` correctly no-op, real Vietnamese
      translations, dubbing, `ready_for_review` with mux correctly skipped),
      opened `ReviewDashboard` (degrades gracefully with no video, no crash
      — "Open Editor" and "Share this video" still incorrectly shown, exactly
      the gap the still-open `hasVideo` guard task below exists to fix), and
      confirmed `Approve & Publish` (the audio-only `approve_job` branch)
      works end-to-end for real. Zero console errors throughout.
      code-reviewer found 2 real gaps, both fixed (re-verified live after):
      the hand-rolled CSV parser read fields by index without checking the
      row actually had the expected column count — a row with an unescaped
      comma inside a value (a realistic authoring mistake) silently
      truncated that value instead of erroring, violating US1's "no
      silently dropped content"; fixed with a per-row column-count check.
      Also: `ProjectDetail.tsx` only ever rendered jobs nested under a
      video — a script-first job became permanently invisible on the
      Project page once the user navigated away from the one-time
      post-submit redirect, contradicting US4. Fixed with a new
      "Script-only jobs" section (`jobs.filter(j => j.source_video_id ===
      null)`), extracting a shared `JobRow` component so both sections stay
      in sync. Also moved `TARGET_LANGUAGE_GROUPS`/`SOURCE_LANGUAGES` out of
      `NewProject.tsx` into a new `frontend/src/lib/targetLanguages.ts` (a
      page importing a constant from another page was a one-off outlier
      per code-reviewer, now 2 pages depend on it) — `lib/` is this
      codebase's established home for shared non-component code.
- [x] `[frontend]` `useCreateScriptJobs()` hook for `POST
      /projects/{id}/script-jobs`, wired into `NewScriptProject.tsx`'s submit —
      file: `frontend/src/hooks/useDialogueLines.ts`. Backend was already
      done, wired directly to the real endpoint (no mocking needed). Done
      together with the `NewScriptProject.tsx` task above (tightly coupled —
      the page's submit handler IS this hook's only caller).
- [x] `[frontend]` A way to reach the new flow from the existing entry points
      (e.g. a second button next to "New Project") — file:
      `frontend/src/pages/Dashboard.tsx` or `ProjectDetail.tsx` (whichever
      already hosts the "New Project" action). Depends on:
      `NewScriptProject.tsx` task.
      Added a "New from Script" button next to "New Project" on
      `Dashboard.tsx`, linking to `/projects/new-script`. Verified live.
- [x] `[frontend]` `ReviewDashboard.tsx`: add a single `hasVideo =
      !!job.source_video_id` guard at the top; when false, render the per-line
      audio review list with no `<video>` element in place of the video
      preview/export controls — file: `frontend/src/pages/ReviewDashboard.tsx`.
      Also fixed the Approve-button conflict already identified in the
      `approve_job` task's review note: `hasVideo === false` now gates on
      "every `safety_status === 'ok'` line has `dubbed_audio_gcs_uri`" (+ at
      least 1 line actually dubbed), mirroring the backend's own 2-branch
      logic exactly, instead of the old universal `flaggedCount > 0` check.
      `useJobVideoUrl` is now only enabled once `hasVideo && current_stage
      === "ready_for_review"` (a new `hasMuxedVideo` flag, also reused for
      `canOpenEditor` and gating `ShareVideoPanel`) — avoids a pointless
      404 fetch attempt for a job with nothing to fetch yet.
- [x] `[frontend]` `VideoEditor.tsx` entry point: hide "Open Editor" when
      `!hasVideo` — file: `frontend/src/pages/VideoEditor.tsx`,
      `frontend/src/pages/ReviewDashboard.tsx`. Depends on: the `hasVideo`
      guard task above (same file/session, bundle together).
      `ReviewDashboard.tsx`'s `canOpenEditor` (via `hasMuxedVideo`) already
      hides the link; added a defense-in-depth guard directly in
      `VideoEditor.tsx` too, for reaching the page by direct URL — `GET
      /jobs/{id}/timeline` degrades gracefully rather than erroring for a
      video-less job (builds a default timeline referencing a video that
      doesn't exist), so this couldn't be caught via the page's existing
      `isError` state alone.
- [x] `[frontend]` "Attach Video" action on `ReviewDashboard.tsx` (reuses the
      existing `POST /projects/{id}/upload-url` flow) + `useAttachVideo()` hook
      for `POST /jobs/{id}/attach-video` **(P2)** — file:
      `frontend/src/pages/ReviewDashboard.tsx`,
      `frontend/src/hooks/useDialogueLines.ts`. Depends on: the backend
      `attach-video` endpoint task, the `hasVideo` guard task.
      Found and fixed a real gap while implementing: `JobOut` (backend
      schema) never exposed `project_id`, which this flow needs to call
      `POST /projects/{project_id}/upload-url` — added it (file:
      `backend/api/schemas/schemas.py`; rebuilt, full suite re-run: 257
      passed, 27 skipped, only the 1 pre-existing unrelated failure). Also
      gave `useJob` a `refetchInterval` that polls while `current_stage ===
      "muxing"` (mirrors the existing "pending redub" polling pattern
      already in this same file) so the UI picks up the mux finishing
      without a manual reload.
      **Verified live end-to-end in a real browser against the real
      backend**: clicked Attach Video on the already-published script-first
      job from the earlier tasks, uploaded a real ffmpeg-generated test
      video (5s, with audio), watched it upload → mux → the UI
      auto-refresh via polling with ZERO manual reload — the preview
      showed the real muxed video with the dialogue subtitle correctly
      burned in ("Xin chào các bạn!"), "Open Editor" and "Share this
      video" reappeared automatically once `hasVideo`/`hasMuxedVideo`
      flipped true, and opening the editor showed the real Video/Original
      Audio/Cat/Dog tracks with real waveforms. Zero console errors
      throughout.

      **A second code-reviewer pass found 2 more real bugs in this same
      work, both fixed and re-verified live (including deliberately
      reproducing a real mux FAILURE, not just the success path)**:
      (a) `hasMuxedVideo` was derived from `current_stage ===
      "ready_for_review"` — but `worker/main.py::_attach_video`'s `finally`
      block resets `current_stage` to `"ready_for_review"` on BOTH success
      AND failure (only `muxed_video_gcs_uri` distinguishes them). A failed
      mux left `source_video_id` set but no muxed video — the old logic
      showed "Open Editor" anyway, triggered a permanent `video-url` 404
      loop, and (combined with the old `!hasVideo`-based Attach-Video
      visibility) gave the user no way to ever retry. Fixed by deriving
      `hasMuxedVideo` from `!!job?.muxed_video_gcs_uri` directly, and by
      changing the backend's `attach_video` guard (file:
      `backend/api/routers/jobs.py`) to block only on
      `muxed_video_gcs_uri is not None` instead of `source_video_id is not
      None` — a failed attach can now be retried with a new upload,
      verified live end-to-end (uploaded a real corrupt file, confirmed in
      worker logs the mux failed with `current_stage` correctly reset,
      confirmed "Attach Video" reappeared, re-uploaded a valid file, watched
      it succeed). 2 new regression tests in `test_jobs.py`.
      (b) The first fix attempt tried signaling "muxing finished" by
      watching the Firestore-backed `useJobStatus` hook instead of polling
      `useJob` directly (per frontend/AGENTS.md's realtime-over-polling
      preference) — reproduced live that this is **genuinely unreliable**
      for this specific transition: `useJobStatus`'s REST-polling fallback
      (the only option in local dev, where the worker skips writing to
      Firestore) samples every 3s, and a fast muxing→ready_for_review round
      trip (e.g. an immediate failure) can complete entirely between two
      samples — the observed value never visibly changes, so nothing
      re-fires, and the UI stayed stuck on "Attaching video..." forever
      despite the backend having already finished. Reverted to polling
      `useJob` directly (file: `frontend/src/hooks/useDialogueLines.ts`),
      gated on `current_stage === "muxing"`, matching the existing "pending
      redub" polling precedent already in that same file — documented why
      in both files so this isn't re-litigated later. Kept `useJobStatus`
      for a narrower, genuinely Firestore-only purpose: surfacing the new
      `muxSucceeded`/`durationWarning` fields (file:
      `frontend/src/hooks/useJobStatus.ts`) the worker already wrote but
      nothing previously read, as an error/warning banner in
      `ReviewDashboard.tsx` — cloud-only (degrades to no extra detail in
      local dev, same as `exportProgress` already does), additive to the
      `useJob`-based polling, not a replacement for it.
      Also fixed `ShareVideoPanel`'s gate from `hasVideo` to `hasMuxedVideo`
      (showing an empty share-link box with no real video was its own small
      instance of the same root confusion). Backend suite re-run clean (258
      passed, 27 skipped, only the 1 pre-existing unrelated failure);
      `npx tsc -b` clean.

      **A third, final confirmation review pass found 1 more small gap**:
      `useAttachVideo`'s `onSuccess` only called `invalidateQueries`, leaving
      a brief window (one HTTP round trip) where the cached `job` still
      showed the pre-attach snapshot even though the backend had already
      committed the `"muxing"` transition — self-healing (the next poll tick
      corrects it, and the backend's own `current_stage != "ready_for_review"`
      guard prevents any actual double-mux), but could show a confusing
      "Attach Video" button for an instant right after a successful click.
      Fixed by writing the mutation's own response directly into the cache
      via `setQueryData` before invalidating (file:
      `frontend/src/hooks/useDialogueLines.ts`) — no second round trip
      needed since the response already carries the fresh `current_stage`.
      That same review pass re-confirmed, after tracing the actual commit
      ordering in `worker/main.py::_attach_video`, that `muxed_video_gcs_uri`
      and `current_stage` always commit together in one transaction (no
      read-race possible), and walked the full state machine confirming
      exactly one of `canAttachVideo`/`canOpenEditor` is ever true in every
      stable state. `npx tsc -b` clean after this fix.

## Infra/Docs

- [x] `[infra/docs]` Update `docs/database-schema.md` — add `jobs.project_id`,
      the nullable `jobs.source_video_id`, `dialogue_lines.segment_code` — file:
      `docs/database-schema.md`. Depends on: the backend migration task.
      Updated the ERD (`PROJECTS ||--o{ JOBS`, `SOURCE_VIDEOS` relation
      changed to optional `|o--o{`), the `JOBS`/`DIALOGUE_LINES` entity
      blocks, added a "Important design notes" bullet explaining the
      `project_id`/nullable `source_video_id`/`segment_code` changes, and
      `CREATE INDEX idx_jobs_project ON jobs(project_id);` to the indexes
      section.
- [x] `[infra/docs]` Update `docs/agent-pipeline.md` — document
      `ScriptBenchmarkAgent`, the runtime stage-agent selection in
      `Pipeline.run()`, and that `"muxed"` becomes conditional (skipped until a
      video is attached) — file: `docs/agent-pipeline.md`. Depends on: the
      `ScriptBenchmarkAgent` and `pipeline.py` backend tasks.
      Added a paragraph after the main flowchart explaining the script-first
      alternative to Agent 1 and the conditional mux stage, a new "Agent 1
      (script-first variant) — English Timing Benchmark Agent" subsection,
      a note on Agent 2's section (safe no-op for script-first jobs), a note
      on the Mux Service section, and a new row in the summary table.
- [x] `[infra/docs]` Document the CSV script import format
      (`segment_code,character_name,text`, encoding, header row expectations)
      with a sample file, referenced from `NewScriptProject.tsx`'s upload UI —
      file: `docs/backend-spec.md` (or `docs/frontend-spec.md`, whichever
      already documents user-facing input formats), a sample `.csv` under
      `docs/`. Depends on: the schemas task (final column names).
      Chose `docs/backend-spec.md` (new "## 6. Script import format" section)
      since the file already documents request-body/data-format concerns,
      and added the new script-first endpoints (`POST
      /projects/{id}/script-jobs`, `GET /characters/voice-options`, `PUT
      /characters/{id}/voice`, `POST /jobs/{id}/attach-video`, `GET
      /jobs/{id}/dubbed-audio-bundle`) to the section 2 endpoint table, which
      had never been updated for this feature. Added
      `docs/sample-script-import.csv` (5 rows, including one with an escaped
      embedded quote) — verified by actually running it through
      `NewScriptProject.tsx`'s real `parseCsvLine`/`parseScriptCsv` logic
      (copied into a scratch script) to confirm it parses to the exact 5
      expected rows with no errors, rather than just asserting it by eye.
- [x] `[infra/docs]` Update `docs/frontend-spec.md` — add the new
      `/projects/:id/new-script` (or equivalent) route and the audio-only
      Review Dashboard variant — file: `docs/frontend-spec.md`. Depends on: the
      `NewScriptProject.tsx` and `ReviewDashboard.tsx` frontend tasks.
      Actual route is `/projects/new-script` (a sibling of `/projects/new`,
      not nested under an existing project id — confirmed in `App.tsx`).
      Added a new "3.2b New Project from Script — Upload" section, a new
      bullet list under 3.4 documenting the audio-only variant (download
      bundle / attach-video / retry-after-failure, and the
      `hasVideo`/`hasMuxedVideo` distinction), and the new page + edge in
      the section 2 flowchart.
      `code-reviewer` found a real gap on this round: the docs described a
      "Download dubbed audio" button in the Review Dashboard that didn't
      actually exist anywhere in the frontend (`grep -rn
      "dubbed-audio-bundle" frontend/src/` had zero matches) — FR-008's
      endpoint was built and tested (see the task above) but never wired to
      any UI, so a script-first job's only deliverable before a video exists
      was unreachable except via a raw `curl`. Rather than walk the docs
      back, implemented the missing button: `useDownloadDubbedAudioBundle`
      (new mutation hook, `frontend/src/hooks/useDialogueLines.ts`, same
      fetch-signed-URL-then-navigate pattern as the existing
      `useDownloadJobVideo`/`ShareVideoPanel.tsx`) + `DownloadAudioBundleButton`
      (new component, `frontend/src/pages/ReviewDashboard.tsx`, shown
      whenever `hasAnyDubbedLine`, next to the Attach Video action). `npx tsc
      -b` clean. Manually verified live: reset a real job's
      `source_video_id`/`muxed_video_gcs_uri` to null via `psql` (local dev
      test data) to reproduce the audio-only state, confirmed the button
      renders, clicked it, confirmed via `docker compose logs api` the
      backend returned `200 OK` for both the signed-URL call and the
      resulting zip download, then copied the actual zip out of the
      container and confirmed via `unzip -l` it contained the real
      `001.wav`/`002.wav`/`003.wav` (217KB) — then restored the job's
      original muxed-video state.
      A second `code-reviewer` pass on this new button/hook found one more
      real issue: it was gated only on `hasAnyDubbedLine`, which is true for
      essentially every dubbed job (video-first ones included), so it would
      have cluttered the header on every already-published, fully
      video-attached job too — not just the audio-only state the docs
      describe. Fixed by adding the same `!hasMuxedVideo` gate
      `canAttachVideo` already uses:
      `{!hasMuxedVideo && hasAnyDubbedLine && jobId && ...}`. Re-ran
      `npx tsc -b` (clean) and re-verified live: reset the test job to the
      audio-only state again — button present; restored its muxed-video
      state — button correctly gone, only "Open Editor" shown.
