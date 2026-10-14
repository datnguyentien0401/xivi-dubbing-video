# Tasks: Video & Audio Editor in the Review Dashboard

> Based on `plan.md` in the same directory. Required MVP = US1 + US3 + US2 (simple level) + US6;
> tasks marked **(P2/stretch)** are only done if time allows (see "Schedule Risk" in `spec.md`).

## Backend

- [x] `[backend]` Alembic migration: add `jobs.timeline_edits` (JSONB, default `{}`)
      and `jobs.exported_video_gcs_uri` (String, nullable) — file: `backend/api/db/models.py`,
      `backend/alembic/versions/`. Depends on nothing, so do it first since every
      other backend/frontend task needs these 2 columns to exist.
- [x] `[backend]` Pydantic schema `TimelineState`/`Track`/`Clip` (including the
      `schema_version` field, see Technical Risks in `plan.md`) — file:
      `backend/api/schemas/schemas.py`. Can be done in parallel with the migration
      above (doesn't depend on the DB migration being finished, just needs the shape
      agreed on).
- [x] `[backend]` `GET /jobs/{id}/timeline` (returns `timeline_edits`, builds a
      default snapshot from `dialogue_lines` if empty) + `PATCH /jobs/{id}/timeline`
      (autosave, validated via the schema above) — file:
      `backend/api/routers/jobs.py`. Depends on: the 2 tasks above.
- [x] `[backend]` `POST /jobs/{id}/export` — set `job.current_stage = "exporting"`
      then publish to Pub/Sub (reusing the existing `publish_job_created`/
      `schedule_local_job_created`) — file: `backend/api/routers/jobs.py`. Depends
      on: the migration task.
      (Extended beyond the original description: the published message uses a
      SEPARATE `type="job_export"` — not reusing `type="job"` — because
      `current_stage="exporting"` is not part of `worker/pipeline.py`'s
      `_STAGE_ORDER`; mixing it into the "job" branch would be misinterpreted and
      would re-run the ENTIRE pipeline from scratch. Added the dispatch branch +
      an `_export_job` placeholder in `worker/main.py` within this same task — see
      the note on the "editor_export_service.py" task below, which will REPLACE
      the body of this placeholder function, not just add to it.)
- [x] `[backend]` Change `GET /jobs/{id}/video-url`, `GET /jobs/{id}/share-url`,
      `POST /jobs/{id}/approve` to prefer reading `exported_video_gcs_uri`, falling
      back to `muxed_video_gcs_uri` — file: `backend/api/routers/jobs.py`. Depends
      on: the migration task. Can be done in parallel with the 2 timeline/export
      endpoint tasks above (touches the same `jobs.py` file but different
      functions — needs careful rebase/merge if done concurrently by 2 people).
      (Added 1 guard beyond the original description: `approve_job` used to be
      able to publish a job that had no result video at all — now it returns 409
      if `_effective_video_gcs_uri` is empty. Did not add a `current_stage` guard
      since that's outside this task's scope.)
- [x] `[backend]` `services/editor_export_service.py` — ffmpeg render from
      `timeline_edits`: split/trim/delete (+ insert a black-frame filler for gaps,
      supporting both ripple and leave-gap modes) on the Video track,
      volume/mute/solo/fade on every audio track, reusing the `asyncio.to_thread`
      pattern from `mux_service.py` — file:
      `backend/worker/services/editor_export_service.py`,
      `backend/worker/services/test_editor_export_service.py` (20 tests, including
      real ffmpeg rendering). Depends on: the `TimelineState` schema task.
      (Different from the original description: does NOT use `_atempo_chain`/
      time-stretch — resizing a clip = changing source_start/source_end, not
      changing playback speed; see plan.md. The `cursor` used to concatenate
      clips/fillers uses the duration ACTUALLY MEASURED by ffprobe after each
      segment, not the value declared on the Clip — discovered during review:
      re-encoding at a constant frame rate rounds to whole frames, and
      accumulating using the declared numbers could drift the video track a few
      frames out of sync with the audio track without crashing.)
- [x] `[backend]` `worker/main.py`: REPLACE the body of `_export_job` (currently a
      placeholder that just copies `muxed_video_gcs_uri` as-is — the
      `type="job_export"` dispatch branch + dedicated lock were already added in
      the `POST /jobs/{id}/export` task, no need to redo that) with a real call to
      `editor_export_service`; write `exportProgress` to the Firestore doc
      `jobs/{job_id}` — file: `backend/worker/main.py`. Depends on: the
      `editor_export_service.py` task.
      **Risk that MUST be handled in this same task** (discovered while reviewing
      the "prefer exported_video_gcs_uri" task): `approve_job` currently does not
      block publishing while `current_stage == "exporting"` — if the creator
      clicks Approve & Publish right while an export is running, the job would
      publish `muxed_video_gcs_uri` (the original AI version) instead of waiting
      for the new exported version. Harmless for now since `_export_job` is just
      a placeholder that copies the muxed file verbatim, but it WILL publish the
      WRONG version once editor_export_service.py actually renders something real.
      Add a `current_stage != "exporting"` guard to `approve_job` (file:
      `backend/api/routers/jobs.py`) in this same task, alongside replacing the
      function body.
- [x] `[backend]` Tests for the `exported_video_gcs_uri` → `muxed_video_gcs_uri`
      priority order on `video-url`/`share-url`/`approve` (GET/PATCH timeline and
      POST export ALREADY have full tests from the 2 earlier tasks, no need to
      repeat them) — file: `backend/api/routers/test_jobs.py`. Depends on: all the
      endpoint tasks above.
      (Already done while implementing the "prefer exported_video_gcs_uri" task
      and the "worker/main.py replace _export_job" task — not a separate
      remaining task, keeping this checkbox here to keep tasks.md in sync.)
- [ ] `[backend]` **(P2/stretch)** "Fit to slot" method on the existing
      `TranslationAgent` (shorten an overly long translation while preserving
      meaning/rhyme) + a dedicated message-handling branch in `worker/main.py` to
      return the suggestion via Firestore — file:
      `backend/worker/agents/translation_agent.py`, `backend/worker/main.py`.
      Depends on: the `worker/main.py` "exporting" branch task (reuses the same
      async + Firestore progress pattern).

## Frontend

- [x] `[frontend]` `stores/editorStore.ts` — zustand store for the
      selected clip/track, playhead, zoom, session undo/redo stack — file:
      `frontend/src/stores/editorStore.ts`. Doesn't depend on the backend, do it
      in parallel from the start.
- [x] `[frontend]` `hooks/useJobTimeline.ts` — TanStack Query for `GET`/`PATCH
      /jobs/{id}/timeline` + debounce before PATCH — file:
      `frontend/src/hooks/useJobTimeline.ts`. Can be done in parallel with the
      backend (mock the response matching the agreed `TimelineState` shape from
      the backend schema task), wiring up the real API once the `GET`/`PATCH
      /jobs/{id}/timeline` endpoints are done. (Wired straight to the real API —
      backend was already done beforehand.)
- [x] `[frontend]` `pages/VideoEditor.tsx` (video preview + timeline layout) +
      route `/jobs/:jobId/editor` + "Open Editor" button on `ReviewDashboard.tsx` —
      file: `frontend/src/pages/VideoEditor.tsx`, `frontend/src/App.tsx`,
      `frontend/src/pages/ReviewDashboard.tsx`. Depends on: `editorStore.ts`.
      (Smoke-tested through a real browser: the button shows correctly when
      ready_for_review, opening the editor fetches the default 3-track timeline +
      video preview loads correctly. The review found 2 issues that were fixed:
      the displayed track/duration was mis-read from editorStore instead of from
      the query data directly — the store is a singleton that doesn't reset on
      job change, which could briefly show the previous job's numbers; and
      `useJobTimeline` was missing `staleTime: Infinity` — a background refetch
      on window focus would call `loadTimeline` and wipe the undo/redo stack once
      US5 is real. Both have been fixed in `useJobTimeline.ts`/`VideoEditor.tsx`.)
- [x] `[frontend]` `components/editor/Timeline.tsx` + `TrackRow.tsx` +
      `Playhead.tsx` — display the Video/Original Audio/character dialogue
      tracks, play/pause, drag/click the playhead, zoom in/out (US1) — file:
      `frontend/src/components/editor/`. Depends on: `VideoEditor.tsx`,
      `useJobTimeline.ts` (can start with mock data while waiting for the real-API
      wiring task).
      (Smoke-tested through a real browser: clicking a clip to select + seek,
      play/pause, zoom in/out all work correctly, video preview stays in sync
      with the subtitles. Found + fixed 1 real bug OUTSIDE this component file's
      scope: `backend/api/routers/local_storage.py` (GET object, used for
      video/audio in local dev) didn't support HTTP Range requests at all —
      the Starlette FileResponse version in use (0.38.6) always returns 200 +
      the whole file regardless of the Range header, which prevented `<video>`
      from seeking (`video.seekable` was always empty `[[0,0]]` even though
      `readyState` had reached HAVE_ENOUGH_DATA — reproduced it firsthand via
      Timeline.tsx: clicking a clip didn't move the playhead). The bug ONLY
      affects local dev — real GCS signed URLs already support Range natively,
      production is unaffected. Added manual Range handling (206 Partial Content
      + Content-Range, 416 for invalid ranges) + 5 new tests in
      `test_local_storage.py` (11/11 passing), rebuilt the `api` container to
      apply it. The review found 1 more real gap: a suffix-range "bytes=-N"
      (the LAST N bytes of the file, used by some media players to read the
      trailing moov atom) was being MISINTERPRETED as "the first N bytes"
      instead of returning an error — fixed + added 3 more tests (14/14
      passing), rebuilt again + verified with a real curl request.)
- [x] `[frontend]` `components/editor/ClipBlock.tsx` + `WaveformClip.tsx` (using
      the existing `wavesurfer.js` from `package.json`) — selecting a clip shows
      original/translated text, cultural safety label, draws the waveform (US1)
      — file: `frontend/src/components/editor/ClipBlock.tsx`,
      `frontend/src/components/editor/WaveformClip.tsx`. Depends on: `Timeline.tsx`.
      (Added 2 files beyond the original list: `components/editor/ClipInspector.tsx`
      — a "side panel" next to the video preview showing original/translated text/
      safety info when a dialogue clip is selected (US1), split out from
      ClipBlock.tsx since it's a completely different responsibility (a detail
      panel, not drawing a clip on the timeline); and `lib/mediaUrl.ts` —
      extracted `resolvePlayableUrl` out of AudioPlayer.tsx (where it was first
      used) since WaveformClip.tsx needed this exact logic a second time,
      avoiding copy-paste. TrackRow.tsx was also switched to rendering
      `ClipBlock` instead of drawing clips inline itself (refactored from the
      US1 task). Smoke-tested through a real browser: the waveform shows on all
      3 dialogue clips, clicking a clip → the panel shows the correct character/
      original/translated/Safe badge, seek+select still work as before, console
      is clean. The review found 2 things to fix: a comment incorrectly
      described the mechanism of "clicks passing through the waveform" — it's
      actually the `pointer-events-none` class on the container, not
      wavesurfer's `interact: false` option (wavesurfer still attaches its own
      click listener regardless of that option) — fixed the comment to be
      accurate; and the comment "storeTimeline is always consistent" in
      VideoEditor.tsx overclaimed — `dialogueLines` is actually a 3rd independent
      source (a separate react-query), theoretically there could be 1 frame of
      mismatch when switching jobs even though it's masked by non-matching
      UUIDs — switched the `loadTimeline()` hand-off from `useEffect` to
      `useLayoutEffect` so React re-renders synchronously before painting,
      removing the risk of a 1-frame flash of wrong data, and corrected the
      comment to be honest instead of overclaiming "absolutely safe". Verified
      again through the browser: clicking directly on the waveform still
      selects/seeks correctly.)
- [x] `[frontend]` Drag a dialogue clip to reposition it + snapping, per-track
      volume/mute/solo, short fade in/out (US3) — file:
      `frontend/src/components/editor/ClipBlock.tsx`,
      `frontend/src/stores/editorStore.ts`. Depends on: `ClipBlock.tsx`.
      (Done. Added `updateClip`/`updateTrack` to `editorStore.ts` (a thin wrapper
      around the existing `applyEdit` — each call = exactly 1 undo step).
      `ClipBlock.tsx`: dragging the clip body to reposition it (pointerdown/move/up
      attached on `window`, mutating `style.left/width` directly per pixel,
      committing to the store only once on pointerup), dragging the left/right
      edges to trim (`source_start`/`source_end`), snapping to the edge of
      another clip/playhead/0/duration_seconds within `SNAP_THRESHOLD_PX`,
      blocking overlap with adjacent clips (move: a greedy collision-resolution,
      not optimized for dense arrangements — acceptable for MVP; trim: only
      blocks up to the adjacent edge). Dragging/trimming is only enabled for
      "dialogue" tracks (`draggable` prop from `TrackRow.tsx`) — video/original
      audio belong to US2 (a separate task below, not done yet). Limitation
      explicitly noted in a comment: trim-end (dragging a clip LONGER) isn't
      clamped against the actual length of the dubbed audio file (the client
      doesn't know it ahead of time) — only overlap + timeline bounds are
      checked; the main case that needs support is SHORTENING, not lengthening.
      `Timeline.tsx`: added Mute/Solo buttons + a volume slider per track (hidden
      for the "video" track, matching the audio-track distinction in the backend
      `_select_audible_tracks`). `ClipInspector.tsx`: extended to show a volume
      slider + 2 fade in/out fields for ANY selected clip (not just dialogue
      clips). `VideoEditor.tsx`: derives `selectedClip`/`selectedTrackId`
      alongside `selectedDialogueLine`, passes `onUpdateClip` down to
      `ClipInspector`. Verified through a real browser (not a unit test —
      drag-and-drop interactions are hard to test meaningfully in an automated
      way): move-drag, trim-drag on both edges, snapping to an adjacent clip
      edge, the volume slider, the 2 fade in/out fields, the Mute/Solo buttons
      all work correctly.

      Found + fixed while self-reviewing the code (the `code-reviewer` subagent
      FAILED to run twice this time — infra issues, session limit then a stall —
      so did a manual review instead): (1) MISSING pointer capture — if the user
      releases the mouse OUTSIDE the browser window while dragging, the
      `pointerup` event never fires, leaving `dragRef` stuck in the "dragging"
      state forever and the `pointermove` listener attached on `window` keeps
      "hijacking" every subsequent mouse movement — fixed with
      `e.currentTarget.setPointerCapture(e.pointerId)` in `startDrag`.
      (2) When a drag gesture falls below the no-op threshold (a slight hand
      tremor while clicking to select) and is skipped without committing, the
      DOM may already have been shifted a few pixels by `handlePointerMove`
      beforehand and would NOT self-correct until some other re-render happens
      (in practice it usually self-corrects very quickly because `ClipBlock`
      subscribes to `playheadSeconds`, but shouldn't rely on luck) — fixed with a
      `resetElementStyle()` function called explicitly on every no-op branch.
      Both fixes were verified again through the browser (`tsc -b` clean,
      drag/trim/click still correct after the fix).)
- [x] `[frontend]` Split/trim/delete video clips + ripple mode + a warning when
      deleting a segment containing a dubbed dialogue line (US2, simple MVP
      level) — file: `frontend/src/components/editor/TrackRow.tsx`,
      `frontend/src/stores/editorStore.ts`. Depends on: `Timeline.tsx`. Can be
      done in parallel with the drag/volume task above (different UI parts of
      the same component tree, but should be owned by 1 person to avoid
      ripple/snap logic conflicts).
      (Done. `editorStore.ts` adds `splitClip(trackId, clipId, atSeconds)`
      (splits 1 clip into 2 at a given position, no-op if too close to either
      end — see `MIN_SPLIT_PIECE_SECONDS`) and `deleteClip(trackId, clipId,
      ripple)`. `deleteClip` handles overlap between the deleted clip and EVERY
      other clip (any track) via an "interval subtraction": a clip FULLY inside
      the cut segment → deleted entirely (correct per spec.md for dialogue
      lines); a clip only PARTIALLY overlapping (e.g. original audio/video
      spanning longer than exactly the deleted segment) → TRIMMED (keeping the
      part outside the cut) instead of deleted entirely — realized while writing
      this: the initial design of "any overlap means full delete" would wipe out
      the entire original audio track even when only cutting a middle segment of
      video, fixed before testing rather than discovered during review.
      `ripple=true` shifts every clip AFTER the cut segment (any track) forward
      + reduces `duration_seconds`; `ripple=false` just leaves a gap in the
      specific track of the deleted clip, other tracks keep their absolute
      position. `ClipBlock.tsx`: split the single `draggable` (boolean) into 2
      separate props `canMove`/`canTrim` — the "video" track can be trimmed
      (dragging the edge, reusing the same trim mechanism from US3) but cannot
      be moved (US2 has no "freely reposition" action for video, only
      split/trim/delete); the "dialogue" track can be both moved and trimmed
      (US3); original audio is out of scope (only 1 full-length clip, no editing
      action yet). `TrackRow.tsx` decides these 2 props based on `track.type`.
      `ClipInspector.tsx`: added a "Video clip" block (a "Split at playhead"
      button — disabled when the playhead is outside the body of the selected
      video clip; 2 buttons Delete + ripple / Delete leave gap) shown only when
      `trackType === "video"`. `VideoEditor.tsx`: derives
      `selectedTrackType`, `canSplitAtPlayhead`; warns about losing dubbed
      dialogue via `window.confirm` (same pattern as
      `Dashboard.tsx`/`ProjectDetail.tsx`) BEFORE calling `deleteClip`, computed
      based on dialogue lines with `dubbed_audio_gcs_uri` that overlap the cut
      segment (without distinguishing full delete vs. partial trim, to keep the
      warning simple at MVP level).

      Verified through a real browser (resetting + reloading between steps to
      avoid mixing up old state): splitting at the playhead in the middle of a
      video clip (tried while it was playing too — discovered the real video
      is actually longer than 0.1s longer than the declared `duration_seconds`,
      harmless, it only makes the Split button auto-disable exactly when the
      playhead passes the clip's boundary instead of splitting incorrectly);
      selecting the second half and clicking Delete + ripple — confirmed the
      "losing a dubbed line" warning shows correctly, and after confirming:
      `duration_seconds` decreases correctly (8.5s→4.0s), the Video track keeps
      exactly the first half, the Original Audio track (previously full-length)
      gets TRIMMED correctly to match the remaining segment (not wiped out
      entirely — confirming the fix above works), the dialogue track: a line
      fully inside the deleted segment is deleted entirely, a line partially
      overlapping is trimmed correctly at the boundary. At one point
      `window.confirm()` accidentally hung the test tab (sending `Enter` via CDP
      doesn't reliably dismiss a native dialog) — manually dismissed it in the
      browser; after that, avoided triggering confirm() via automation again in
      this session, relying on the earlier successful ripple test to infer that
      the `ripple=false` branch is also correct (it shares exactly the same
      "interval subtraction" code path, differing only in the final shift step).

      The `code-reviewer` subagent could NOT be run this time (the 2 previous
      attempts for the US3 task already failed due to infra issues — session
      limit then a stall — so decided to go straight to manual review for this
      task instead of trying a 3rd time); manually re-read all of
      `deleteClip`/`splitClip` branch by branch (overlapping on both sides, left
      only, right only, no overlap, exact boundary) and confirmed it matches the
      behavior observed in the browser.)
- [x] `[frontend]` Wire up real autosave (debounced PATCH via `useJobTimeline`)
      replacing the temporary local state — file: `frontend/src/pages/VideoEditor.tsx`.
      Depends on: the backend `PATCH /jobs/{id}/timeline` being done + the 2
      US2/US3 tasks above.
      (Done. `VideoEditor.tsx`: added a `useEffect` watching `storeTimeline`
      (from editorStore), calling `scheduleAutosave(storeTimeline)` whenever its
      reference changes (= a new edit happened, `applyEdit` always creates a new
      object) AND it differs from the just-fetched version — avoiding a
      redundant autosave right when the editor first opens. Deliberately did NOT
      include `scheduleAutosave`/`timeline` in the dependency array (only
      `storeTimeline`) — if `scheduleAutosave` (a new function on every render)
      were included, the debounce timer would keep resetting continuously while
      the video plays (playheadSeconds changes constantly via onTimeUpdate →
      re-render → effect reruns), which would mean autosave never gets a chance
      to run while reviewing what was just edited — this bug was caught BEFORE
      writing the code, not during testing.

      Found + fixed 2 more important issues at DESIGN time (before the first
      test run, not during review afterward):
      (1) The old `useLayoutEffect` called `loadTimeline(timeline)` whenever
      `timeline` (the query data) changed — but after a successful autosave
      PATCH, `useAutosaveTimeline`'s `onSuccess` calls `setQueryData` to write
      the server's returned snapshot back into THIS SAME query, which changes
      `timeline`'s reference even though the content is identical to what was
      just saved; without a guard, `loadTimeline()` would WIPE OUT the
      undo/redo stack + selection + playhead AFTER EVERY SUCCESSFUL AUTOSAVE.
      Fixed with a `loadedJobIdRef` — only calls `loadTimeline()` ONCE per
      `jobId`, not every time the query data happens to change.
      (2) The "Saving…/Saved" indicator in the header was INITIALLY written by
      comparing the `storeTimeline !== timeline` reference — but testing
      through the browser revealed it got STUCK ON "Saving…" forever after the
      first edit, because the 2 objects NEVER share the same reference even
      after a successful PATCH (the PATCH response is a freshly
      JSON.parse'd object, not the same instance as the snapshot that was
      sent) — fixed by exposing `isSaving` directly (= either waiting on the
      debounce OR a request in flight) from `useAutosaveTimeline` itself (added
      an `isScheduled` state in `useJobTimeline.ts`) instead of inferring it via
      a reference comparison on the caller side.

      Verified through a real browser: editing 1 clip (drag-trim + change
      volume) → the header indicator shows "Saving…" immediately, then switches
      to "Saved"; reloading the page (a hard reload, not HMR) → confirmed the
      edited state (the trimmed clip width, the volume value) is still intact —
      proving the whole debounce → PATCH → DB save → GET-back cycle works
      end-to-end. Hit 2 "X is not defined" errors (once for useEffect, once for
      useState) purely due to Vite HMR serving a stale module after editing
      files in quick succession — confirmed via a direct `grep` of the file on
      disk + a clean `tsc -b` each time, not a real bug; it resolved itself
      after navigating again (a full reload) instead of relying on HMR.)
- [x] `[frontend]` `hooks/useExportJob.ts` + an Export button + showing progress
      via the existing Firestore listener (`useJobStatus.ts`) + a preview before
      clicking Approve & Publish (US6) — file: `frontend/src/hooks/useExportJob.ts`,
      `frontend/src/pages/VideoEditor.tsx`. Depends on: the backend `POST
      /jobs/{id}/export` + the `worker/main.py` branch being done.
      (Done. `useJobStatus.ts`: added an `exportProgress` field to `JobStatus`
      (read from the Firestore doc, see worker `_update_export_status`).
      `VideoEditor.tsx`: an Export button in the header (`useExportJob`),
      disabled while exporting OR while there are still edits that haven't
      finished autosaving (`isSaving` — export reads `timeline_edits` directly
      from Postgres, exporting right while debouncing would render from an
      OLDER version than what's shown on screen). Detects when the export has
      ACTUALLY finished (not just when the POST got a 202 accepted — the worker
      changes currentStage only after ffmpeg finishes) by comparing
      `exported_video_gcs_uri` before/after via
      `queryClient.invalidateQueries(...).then(...)` + reading the cache
      directly (not using an effect watching `[job]`, because TanStack Query v5
      has structural sharing enabled — a FAILED export branch wouldn't change
      the `job` reference, so that effect would never fire correctly for the
      error branch). On a successful export, it auto-invalidates
      `job-video-url` so the `<video>` preview automatically switches to the
      new version (US6 "preview the exported version") — the Approve & Publish
      button stays in ReviewDashboard.tsx, not duplicated here.

      3 real bugs found while testing through the browser (not during review
      afterward): (1) The old `useExportJob.ts` invalidated
      `["job-timeline", jobId]` immediately on POST /export being accepted —
      but the job had just switched to `current_stage="exporting"`, so GET
      /jobs/{id}/timeline returns 409 during that window, which made
      VideoEditor.tsx's `useJobTimeline` fall into isError and replaced the
      whole editor screen with "Could not open editor" the moment Export was
      clicked — removed this invalidation (no longer needed now that real
      autosave exists, editorStore is already the source of truth).
      (2) `useJobStatus.ts`'s polling fallback (used when Firestore has no data
      — exactly the case when `USE_LOCAL_FIRESTORE=true` in dev) PERMANENTLY
      turned off refetchInterval the first time it saw
      `current_stage==="ready_for_review"` — correct for the original dubbing
      pipeline (one-directional progress, stops there for good) but WRONG for
      export (a job can leave and then RETURN to that same stage multiple
      times: each export is a ready_for_review→exporting→ready_for_review
      cycle) — turning off polling right away meant the UI would never see
      subsequent "exporting" states. Fixed to poll at a fixed 3s interval
      whenever still enabled (enabled auto-turns-off entirely once real
      Firestore is active, costing nothing extra in the cloud).
      (3) The RUNNING worker container was still the OLD image — `_export_job`
      was still the placeholder version (copying `muxed_video_gcs_uri` straight
      into `exported_video_gcs_uri`, not yet calling the real
      `editor_export_service.py`) even though the real code already existed on
      disk — the image had never been rebuilt after that service was finished.
      Not a code bug, but it completely blocked testing the feature correctly —
      ran `docker compose up -d --build worker` to run the real version.

      Verified through the browser + directly checking the DB/Docker logs:
      clicking Export → the button switches to "Exporting…" (disabled) → a
      green banner "Export complete — preview updated below." →
      the `<video>` preview switches to the newly exported version (the frame
      content visibly changes) → the DB confirms `exported_video_gcs_uri`
      points to the correct new file under `jobs/{id}/exported/` (not the
      old `muxed/` one).)
- [ ] `[frontend]` **(P2/stretch)** Undo/Redo (Ctrl/Cmd+Z, Shift+Ctrl/Cmd+Z) + a
      "Restore AI version" button (whole job or 1 clip) — file:
      `frontend/src/stores/editorStore.ts`,
      `frontend/src/components/editor/Timeline.tsx`. Depends on: the real
      autosave task being wired up.
- [ ] `[frontend]` **(P2/stretch)** Re-dub a single line directly in the editor
      (reusing the existing `useUpdateDialogueLine` mutation from
      `ReviewDashboard.tsx`) + UI showing the "Fit to slot" suggestion to
      approve before re-dubbing — file: `frontend/src/pages/VideoEditor.tsx`.
      Depends on: `ClipBlock.tsx` + the backend "fit to slot" task.

## Infra/Docs

- [ ] `[infra/docs]` Update `docs/database-schema.md` — add the 2 new `jobs`
      columns (`timeline_edits`, `exported_video_gcs_uri`) — file:
      `docs/database-schema.md`. Depends on: the backend migration task.
- [ ] `[infra/docs]` Update `docs/agent-pipeline.md` — note that the
      `"exporting"` stage is outside `_STAGE_ORDER` and that
      `editor_export_service.py` is not a Gemini Agent — file:
      `docs/agent-pipeline.md`. Depends on: the backend
      `editor_export_service.py` task + the `worker/main.py` branch.
- [ ] `[infra/docs]` Update `docs/frontend-spec.md` — add a section on the
      `/jobs/:jobId/editor` route and the Video Editor's main interactions —
      file: `docs/frontend-spec.md`. Depends on: the basic US1/US2/US3/US6
      frontend tasks being done.
- [ ] `[infra/docs]` Measure actual export time on a video ≤5 minutes (demo),
      consider raising the Cloud Run timeout specifically for `worker` if
      exporting is slower than expected — file: `infra/deploy.sh`. Depends on:
      the backend `worker/main.py` "exporting" branch working locally.
