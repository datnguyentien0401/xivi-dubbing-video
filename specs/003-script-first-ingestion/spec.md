# Spec: Script-First Job Creation

> Extends the core pipeline (`001-dubbing-pipeline`): lets a job start from a locked
> text script instead of requiring an already-recorded source video, so translation
> and dubbing can run in parallel with video production instead of waiting for it.

## User Story
As a content creator, I want to start a dubbing job directly from a locked English
script (no video required yet), so that translation and AI voiceover can happen in
parallel with the video editor's work, instead of waiting until a finished video
exists before any localization can begin.

## Problem Statement
`docs/manual-dubbing-process.docx` describes the manual process this tool is meant
to replace, and it is explicitly optimized around parallel execution: once the
English script is locked, translation and "Renvoice" (AI voiceover) for every target
language happen immediately — in parallel with the video editor cutting footage —
and the finished audio files are handed to the editor afterward to sync against
picture. The editor is never blocked waiting for localization, and localization is
never blocked waiting for a finished edit.

Today's pipeline (`001-dubbing-pipeline`) requires the opposite order: a job can only
be created against an already-uploaded source video, because Agent 1 transcribes
dialogue lines (with timestamps) directly from that video's audio. There is no way
to begin translation before a video exists. For any production where the script is
locked well before the edit is finished — which, per the manual process doc, is the
normal case today — this tool is currently SLOWER than the process it's meant to
replace, because it removes the parallelism the manual process depends on.

## Terminology
- **Script-first job**: a job created from an uploaded text script, with no source
  video attached yet.
- **Video-first job**: a job created the existing way (`001-dubbing-pipeline`),
  starting from an already-uploaded source video.
- **Benchmark audio**: an English reference voiceover of a dialogue line, generated
  by the tool itself (not extracted from any recording), whose natural spoken
  duration stands in for the "real" duration a video-first job would normally
  measure from the source video. This mirrors the manual process's own
  "English Renvoice is the duration benchmark" rule.
- **Attach video**: the action of linking a real source video to an already
  dubbed script-first job, once the editor's cut becomes available.

## User Scenarios (by priority)

### US1 — Create a job from a script, not a video (P1)
I have a locked English script and want to start localization immediately, without
waiting for the video editor to finish cutting footage.
- I upload a structured script (one row per dialogue line: a segment identifier,
  the speaking character's name, and the English text).
- I pick the target language(s) the same way I do today for a video-first job.
- The system validates the script (no duplicate segment identifiers, no empty
  required fields) and shows me a clear error if something doesn't parse, instead
  of silently dropping or misreading rows.

**Acceptance Criteria**
- No video upload is required anywhere in this flow.
- Every row in a valid script becomes exactly one dialogue line — no merging,
  splitting, or silently dropped rows (mirrors the manual process's "preserve
  100% of rows" rule).

### US2 — Assign each character's voice manually before dubbing (P1)
Agent 2 (voice profiling) normally picks a voice by listening to a real audio
sample from the source video — there is no such sample yet for a script-first job.
- For each distinct character found in the uploaded script, I assign a voice myself
  (reusing the existing character/voice management experience), before the job is
  allowed to proceed to translation.
- A job cannot proceed past this point with any character left unassigned.

**Acceptance Criteria**
- A character's assigned voice is used consistently for every line attributed to
  that character within the job.
- If a character with the same name was already assigned a voice in a prior job in
  the same project/series, that choice is offered as the default (keeping voices
  consistent across a series stays a core promise of the product, not something
  this feature is allowed to regress).

### US3 — Get a timing benchmark without a real recording (P1)
Dubbed lines need some target duration to be paced against — normally the source
video's real audio supplies this. A script-first job has no such recording.
- The system generates its own English benchmark audio for every line and uses its
  measured spoken duration as that line's target duration, laid out back-to-back to
  form a full synthetic timeline for the job — the creator does not have to
  estimate or type in any duration by hand.
- If generating the benchmark audio fails for a particular line, that single line
  is skipped with a clear warning rather than failing the whole job (consistent
  with how the existing pipeline already treats a single bad transcript line).

**Acceptance Criteria**
- Every target-language dub is time-stretched against this synthetic timeline the
  same way a video-first job's dub is time-stretched against the real video's
  timeline — no separate, second time-matching mechanism is introduced.

### US4 — Review and receive dubbed audio before any video exists (P1)
Once translation, safety review, and dubbing finish, I should be able to review and
hand off the result exactly as the manual process's own handoff step works: a set
of named audio files per target language, not a finished video.
- I can listen to each dubbed line and edit its translated text / safety status,
  the same review experience as a video-first job, just without a video player.
- Once I approve, I can obtain the full set of dubbed audio files for that
  language, named so they can be matched back to the script's segment identifiers
  (mirrors the manual process's "name the audio files into folders, hand off to
  the editor" step).

**Acceptance Criteria**
- A script-first job can reach a fully reviewed, approved state with zero video
  ever having been uploaded.
- The exported audio set is organized/named so a human editor can unambiguously
  match each file back to its row in the original script.

### US5 — Attach a real video once the edit is finished (P2)
Later, once the video editor finishes cutting footage to match the already-dubbed
audio (the direction of work the manual process itself specifies — the editor
syncs picture to the finished audio, not the other way around), I want to bring
this job to the same finished state as a video-first job.
- I attach the finished video to the job.
- The system produces the same final muxed video a video-first job would produce,
  reusing the dubbed audio that already exists — translation, safety review, and
  dubbing are not re-run.
- If the attached video's total duration is substantially different from what the
  job's synthetic timeline expected, I am clearly warned rather than silently
  getting a desynced result.

**Acceptance Criteria**
- Attaching a video is the only new step required to take an already-dubbed
  script-first job to the same finished state a video-first job reaches today.
- Attaching a video never re-triggers translation, safety review, or dubbing for
  lines that are already dubbed.

## Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-001 | Create a job from an uploaded structured script instead of a source video | P1 |
| FR-002 | Validate the script up front (duplicate/missing identifiers, empty required fields) with clear errors | P1 |
| FR-003 | Require every distinct character in the script to have a manually assigned voice before the job proceeds; default to a character's prior voice choice from an earlier job in the same project when one exists | P1 |
| FR-004 | Generate an English benchmark voiceover per line and derive that line's target duration from it, with no manual duration entry required | P1 |
| FR-005 | Skip (not fail) a single line whose benchmark generation fails, with a clear warning | P1 |
| FR-006 | Translation, cultural safety review, and dubbing behave identically to a video-first job once the synthetic timeline and assigned voices exist | P1 |
| FR-007 | Allow full human review (text edit, safety status edit, re-dub) and approval on an audio-only job, with no video player required | P1 |
| FR-008 | Produce a named, downloadable set of dubbed audio files per target language once approved, traceable back to the script's segment identifiers | P1 |
| FR-009 | Allow attaching a real video to an already-dubbed script-first job, producing the same final muxed video a video-first job produces, without re-running translation/safety/dubbing | P2 |
| FR-010 | Warn (not silently proceed) when an attached video's duration substantially mismatches the job's synthetic timeline | P2 |

## Key Entities (conceptual level)
- **Script import** — the uploaded script: one row per dialogue line, each with a
  segment identifier, a character name, and English text. Becomes the same
  dialogue-line records the pipeline already works with elsewhere — no new,
  separate data model, just a different origin for the data that normally comes
  from transcription.
- **Character voice assignment** — a character now may have a voice chosen directly
  by a human, with no voice sample required, as a valid alternative to the
  existing "Gemini listens to a sample" path.
- **Benchmark duration** — a per-line duration derived from generated English audio,
  standing in for the "measured from the real video" duration a video-first job
  uses. Purely a source of the same duration value the rest of the pipeline
  already expects — not a new kind of timing concept downstream agents need to
  know about.
- **Job video attachment** — the action, and resulting state change, of a
  script-first job gaining a real source video after having already been dubbed.

## Success Criteria
- [ ] A job can be translated, safety-reviewed, and dubbed end-to-end for every
      target language with zero video ever uploaded.
- [ ] A character's manually assigned voice is used on every line attributed to
      that character, with no inconsistency within the job.
- [ ] The exported per-language dubbed audio set can be unambiguously matched back
      to the original script by a human editor with no additional explanation
      needed.
- [ ] Attaching a real video to an already-dubbed job produces a correctly synced
      final video without re-running any already-completed stage.
- [ ] Creating a script-first job and reaching full dub completion takes no longer
      than the equivalent video-first flow, for the same number of lines.

## Edge Cases
- A script row names a character that has never been seen before in the
  project/series → must still require a fresh voice assignment, not silently
  fall back to a default voice.
- Two rows share the same segment identifier → rejected at upload, not silently
  overwritten.
- Benchmark audio generation fails for every single line (e.g. a quota outage) →
  must surface a clear, job-level error rather than silently producing an
  effectively empty job (mirrors the existing "all lines failed" guard already in
  place for transcription).
- An attached video is noticeably shorter/longer than the sum of the job's
  synthetic per-line durations → warn clearly; does not block attaching, but does
  not silently proceed as if nothing were wrong either.
- A user attaches a video to a script-first job BEFORE it has been fully dubbed →
  blocked with a clear message (attaching is only meaningful once dubbing for that
  language is complete, see US5).

## Out of Scope (this version)
- Automatic frame-level alignment between the eventually-edited video and the
  already-dubbed audio — per the manual process itself, the editor is responsible
  for cutting footage to match the finished audio, not the other way around; this
  feature does not attempt to automate that editorial step.
- Native `.xlsx` parsing — script upload accepts a plain structured text format
  (e.g. CSV) in this version; a user with an Excel file exports it to that format
  first.
- Re-running voice profiling against real audio once a video is later attached
  (i.e. automatically refining a manually-chosen voice once a real sample becomes
  available). May be considered as a later, separate enhancement.
- Translating multiple target languages in a single combined pass — each target
  language remains its own independent job, consistent with the one-job-per-language
  architecture already established in `002-video-editor`.
- Any change to the target-audience register/style of the translation and safety
  prompts themselves (tracked separately, not part of this feature).

## Decisions Finalized (2026-10-10)

| # | Issue | Decision | Consequence |
|---|---|---|---|
| 1 | Voice profiling with no audio sample | **Manual voice assignment per character**, reusing the existing character/voice management experience; no Gemini audio-matching step runs for these characters | Simpler, deterministic, and immediately available with existing building blocks; forgoes an automatic "closest-sounding voice" step for script-first jobs specifically |
| 2 | Timing benchmark with no real recording | **Generate English benchmark audio via Gemini TTS per line**; its measured duration is the line's target duration | Matches the manual process's own benchmark rule; costs one extra Gemini TTS call per line beyond what a video-first job needs, but removes any manual duration estimation from the creator |
| 3 | Deliverable before a video exists | **A named, downloadable set of per-language dubbed audio files**; the final muxed-video stage becomes conditional, running only once a video is later attached | Matches the manual process's actual handoff artifact; a script-first job can be fully reviewed/approved on audio alone, with video attachment as a distinct, later step (US5) |

No open questions remain blocking the `/speckit.plan` step.

## Related Constraints (cross-check against constitution.md)
- **What role does Gemini play**: two genuine, additional uses of Gemini beyond
  the existing pipeline — generating the English benchmark voiceover per line
  (reusing the same Native Audio dubbing capability already used for target-
  language dubs, just applied to English itself as a timing reference), and the
  existing translation/safety/dubbing stages, unchanged. This is meaningful use,
  not a workaround.
- **Human approval step**: unchanged — a script-first job still requires explicit
  human review and approval before being considered finished, same as a
  video-first job; only the review surface differs (audio-only, no video player,
  until a video is attached).
- **Deployability**: no new infrastructure category introduced (still Cloud
  Run/Firebase-compatible); script upload and audio-bundle download are ordinary
  file-handling additions to the existing API/worker split.
