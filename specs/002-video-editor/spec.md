# Spec: Video & Audio Editor in the Review Dashboard

> Extends the Review Dashboard screen from `001-dubbing-pipeline`: once the dubbing
> pipeline finishes, the creator can view, cut, and edit the video/audio directly in
> the browser using a familiar timeline-editor style (referencing the Canva editor
> experience), before approving and publishing.

## User Story
As a content creator for children's animated content, I want to edit the already-dubbed
video directly (cut sections, retime dialogue, adjust volume, drag-and-drop audio
clips) right in the dashboard, so that I don't have to export the video to a separate
editing tool every time the AI dub isn't quite right.

## Clarifications

### Session 2026-10-06
- Q: How should Timeline/Track/Clip edits (and undo/redo history) for each job be
  stored on the backend? → A: A single JSONB column on the jobs table
- Q: Where should the "autosave" feature in US5 write to — only the browser
  (localStorage), or send it to the backend? → A: Debounced PATCH to the backend
  (a few seconds after each change), not just localStorage
- Q: Once a job has been published, can the creator reopen the editor to keep
  editing and export/publish again? → A: Yes — allow reopening and republishing
  (not locked read-only after publish)
- Q: What language should the new editor UI (button labels, tooltips, warnings...)
  be written in? → A: English, consistent with the rest of the app's UI (already
  translated)
- Q: What specific time threshold should "responds immediately, with no
  noticeable lag" mean for drag/cut/volume operations? → A: Under 100ms
  (client-side response, excluding backend/Gemini round-trip time)

## Problem Statement
AI-generated dubs are rarely perfect on the first try: a line of dialogue is
longer than the character's on-screen window, a translated segment needs to be
cut, or a character's voice volume is off-balance. Currently the Review Dashboard
(spec 001) only allows editing the text of each line and re-dubbing it. Any
editing of timing, cutting, or audio has to be exported to an external tool,
breaking the "review then publish" flow and losing the connection to the
character/dialogue-line data the system already manages.

## Terminology
- **Timeline**: the time axis showing the tracks of 1 job (1 target language).
- **Track**: 1 lane on the timeline. Includes: Video, Original Audio (background
  music/sound effects), and 1 dialogue audio track per character.
- **Clip**: 1 segment on a track. A dialogue clip corresponds to exactly 1
  dialogue line (`dialogue_line`).
- **AI version**: the result produced by the pipeline, always kept as the
  restore point.

## User Scenarios (by priority)

### US1 — View the dubbed video alongside the timeline (P1)
I open a job in the `ready_for_review` state, and see the video player with a
timeline below it showing the Video track, Original Audio track, and the
per-character dialogue tracks.

- Play/pause, seek by clicking or dragging the playhead on the timeline, zoom
  the timeline in/out.
- Click a dialogue clip → the video jumps to that exact moment, a side panel
  shows the original text, translated text, and the cultural safety label
  (with reasoning) for that line.
- A waveform is shown on audio clips so I can see where there's sound.

**Acceptance Criteria**
- The video and every audio track play in sync; the offset is not noticeable
  to a normal ear.
- Clicking a dialogue clip moves the playhead to that line's exact `start_time`.

### US2 — Cut the video (P1)
I want to remove a scene that isn't appropriate for the target market, or
shorten a segment.

- **Split** the video clip at the playhead position.
- **Trim** the start/end by dragging the clip's edges.
- **Delete** a segment, with a "ripple" option: subsequent clips automatically
  shift to fill the gap, or the gap is left in place.
- When cutting the video, dialogue clips that fall within the cut segment are
  handled according to the corresponding ripple mode (deleted/shifted together),
  and I see a clear warning if the operation would remove dialogue.

**Acceptance Criteria**
- After cutting, the total timeline duration updates correctly; no unintended
  black/silent gaps in ripple mode.
- A warning is shown before deleting a segment that contains a dubbed dialogue line.

### US3 — Edit audio and drag-and-drop (P1)
I want to retime things and balance the audio.

- **Drag and drop** a dialogue clip left/right to reposition it; drag an edge
  to shrink/stretch its duration where allowed (see US4).
- **Volume** per clip and per track; **mute** and **solo** per track.
- Short **fade in/out** for a clip.
- A dialogue clip always stays within its own character's track, it cannot be
  dragged into another character's track (see Out of Scope).
- Snapping to the start/end of other clips and to the playhead for quick
  alignment.

**Acceptance Criteria**
- Can be previewed immediately after editing, without waiting to re-render the
  whole video.
- Clips cannot be dragged to overlap each other within the same dialogue track.

### US4 — Edit by dialogue line with AI assistance (P2)
This is the part that distinguishes XIVI's editor from a regular editor,
because it connects the timeline to the AI data.

- Select a dialogue clip → edit the translated text in the side panel →
  click **Re-dub** for just that line (without re-running the whole
  pipeline); the new clip replaces the old one in place.
- **Fit to slot**: if the dubbed audio is longer than the original dialogue
  line's time slot, the system suggests a shorter translation (rewritten by
  Gemini, still preserving meaning/rhyme if it's a song) for me to approve
  before re-dubbing.
- A line flagged for cultural safety is shown directly on the clip
  (border/label), clicking it shows the reasoning.

**Acceptance Criteria**
- Re-dubbing one line does not change any other clips.
- The "Fit to slot" suggestion is always just a suggestion; it never replaces
  the line automatically until I accept it.

### US5 — Undo, autosave, restore the AI version (P2)
- **Undo/Redo** with multiple steps (Ctrl/Cmd+Z, Shift+Ctrl/Cmd+Z).
- **Autosave**: closing the tab or losing connection and reopening still shows
  the exact state I was editing. Autosave writes to the backend (debounced
  PATCH a few seconds after each change, into the job's JSONB column — see
  Clarifications and the Key Entities section), not just saved locally in the
  browser, because the **Export** button (US6) needs the backend to read the
  latest edit state to render the final video.
- **Restore the AI version** for the whole job or for a single clip.
- The original video and AI-generated audio are **never overwritten**; every
  edit is a layer applied on top.

### US6 — Export the video and approve (P2)
- Click **Export** to produce the final video file from the current timeline;
  see real-time export progress.
- Listen to/watch the exported preview before clicking **Approve & Publish**
  (the rule still holds: no publishing without approval).
- Each target language is its own job, with its own timeline.
- A `published` job can still be reopened in the editor (not read-only) —
  keep editing, export a new version, then click **Approve & Publish** again.
  Each republish is still an active human approval action (it is not
  auto-republished after an edit), per Principle II of the constitution.

### US7 — Subtitles and background music (P3, do if time allows)
- A subtitle track auto-generated from `translated_text`, timed to the
  dialogue clips, that can be toggled on/off and have its font size changed.
- Adjust/lower the volume of the background music (Original Audio track)
  with a slider.

## Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-001 | Show a timeline with Video, Original Audio, and one dialogue track per character | P1 |
| FR-002 | Play video and all tracks in sync; the playhead can be dragged/clicked | P1 |
| FR-003 | Split, trim, delete video clips; support ripple mode | P1 |
| FR-004 | Drag-and-drop a dialogue clip to reposition it within the same track; snapping | P1 |
| FR-005 | Adjust clip/track volume, mute, solo, fade in/out | P1 |
| FR-006 | Selecting a dialogue clip shows original/translated text, the cultural safety label and reasoning | P1 |
| FR-007 | Edit text and re-dub a single line, replacing the clip in place | P2 |
| FR-008 | "Fit to slot": suggest a shorter translation via Gemini, user accepts/rejects | P2 |
| FR-009 | Undo/redo, autosave, restore the AI version (whole job or per clip) | P2 |
| FR-010 | Export the final video from the timeline at the source video's original resolution, with progress and a preview | P2 |
| FR-011 | Only publish after the user actively approves the exported version; a published job can still be reopened in the editor and republished, each republish still requires its own active approval (not automatic) | P1 (constraint carried over from 001) |
| FR-012 | Auto-generated subtitle track; background music adjustment | P3 |

Performance threshold for FR-002 through FR-005 (play/seek, drag-and-drop, cut,
volume adjustment): client-side response under 100ms, excluding time spent
calling the backend/Gemini (see Clarifications).

## Key Entities (conceptual level)
- **Timeline** — belongs to a job; contains a list of tracks, total duration, version.
- **Track** — type (video / background music / dialogue for character X),
  mute/solo state, volume.
- **Clip** — belongs to a track; start position on the timeline, the source
  segment used, volume, fade; a dialogue clip is linked to a `dialogue_line`.
- **Edit** — the list of changes applied on top of the AI version; this is what
  gets autosaved, undone/redone, and restored.

**Storage (see Clarifications)**: the Timeline/Track/Clip and edit history
(undo/redo) of a job are stored together in **a single JSONB column on the
`jobs` table** (no separate relational tables for
`timelines`/`tracks`/`clips`/`edit_history`), validated via a Pydantic model at
the API boundary on read/write. The original AI version
(`muxed_video_gcs_uri`, per-line audio) stays unchanged; the JSONB column only
holds the edit layer applied on top, consistent with the spirit of "the
original video and AI-generated audio are never overwritten" (US5).

## Success Criteria
- [ ] The creator can cut a video segment, retime a dialogue clip, and export a
      new file in under 3 minutes for a short episode (≤5 minutes), without
      leaving the dashboard.
- [ ] Every drag/cut/volume operation responds in the UI under 100ms
      (client-side only, excluding backend/Gemini round-trip time).
- [ ] Closing the browser mid-edit and reopening it does not lose any edits.
- [ ] Restoring the AI version always returns exactly the original pipeline
      output.
- [ ] The exported file has picture and sound in sync, matching what was
      heard/seen on the timeline.
- [ ] The exported file keeps the original video's resolution, with no
      quality downgrade from the source.
- [ ] Cutting/editing in one language's job does not change the timeline of
      another language's job.

## Edge Cases
- Cutting the video removes a dubbed dialogue line → shows a warning; undo
  restores it.
- Dragging a dialogue clip past the end of the video's duration → blocked or
  automatically clamped to the duration.
- Re-dubbing a single line fails (Gemini error/rate limit) → keeps the old
  clip, shows a clear error, allows retry.
- Two people opening the same job at once → not handled in this version (see
  Out of Scope); behavior in that case is not guaranteed.
- Video longer than the MVP limit (30 minutes, per 001) → rejected immediately
  when opening the editor, with a reason shown.
- Slow network while loading the preview → shows a loading state, the
  timeline must not appear "frozen" with no feedback.

## Out of Scope (this version)
- Scene transitions, color filters, stickers, animated text/graphics, Canva-style
  design templates.
- Compositing multiple overlapping video layers (picture-in-picture), keyframe
  animation.
- Changing a dialogue clip's character, or dragging a clip between different
  character tracks.
- Real-time multi-user collaborative editing on the same timeline.
- Editing on mobile devices (desktop browser only).
- Handling multiple people opening/editing the same job at once (blocking
  concurrent edits, merging changes). Deferred for later discussion;
  temporarily assumes only one person edits a given job at a time.
- A "apply this cut to other languages" button (each language is edited
  independently; may be considered after the competition).
- Choosing an export resolution (720p/1080p...); the export always uses the
  original resolution.
- AI-based separation of background music from original dialogue audio (uses
  the original audio track as-is, this limitation is explicitly noted).

## Decisions Finalized (2026-10-06)

| # | Issue | Decision | Consequence |
|---|---|---|---|
| 1 | Which language(s) a video cut applies to | **Each language edited independently** | Each job (one target language) has its own independent timeline. Cutting the Indonesian version doesn't affect the Philippines version; making the same cut elsewhere requires redoing it per job. |
| 2 | Export resolution | **Keep the original resolution** | No 720p/1080p option. Using a lighter-weight version for smooth in-editor preview is a technical decision for the plan stage, as long as the exported file is at the original resolution. |
| 3 | Duration limit while editing | **Keep the same as spec 001: max 30 minutes** | No separate lower threshold set for the editor. The editor on a 30-minute video is much heavier than on a short episode; performance risk is handled at the plan stage; the demo should use an episode ≤5 minutes. |
| 4 | Multiple people opening the same job | **Deferred, to discuss later** | Moved to Out of Scope; temporarily assumes only one person edits a given job at a time. |

No open questions remain blocking the `/speckit.plan` step.

## Related Constraints (cross-check against constitution.md)
- **What role does Gemini play**: the editor itself is a UX feature, not an AI
  core. The part with meaningful Gemini use is US4: re-dubbing by line and
  "Fit to slot" (rewriting a shorter translation while preserving
  meaning/rhyme). If time is short, prioritize US4 over pure video-editing
  conveniences, since this is the part that supports the "Meaningful use of
  Gen AI" score (40%).
- **Human approval step**: unchanged; the editor is where the human
  intervenes, and the Publish button only unlocks after an exported version
  has been approved.
- **Transparency of AI decisions**: the cultural safety label and reasoning
  must be shown directly on the clip in the timeline.
- **UI language**: all editor UI (labels, tooltips, warnings, buttons) is
  written in English, consistent with the rest of the app's UI.
