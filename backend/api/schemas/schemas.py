"""Pydantic schemas for requests/responses — every endpoint must use these,
never return raw dicts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class ProjectCreate(BaseModel):
    name: str
    source_language: str


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1)


class ProjectOut(BaseModel):
    id: UUID
    name: str
    source_language: str
    created_at: datetime

    class Config:
        from_attributes = True


class UploadUrlRequest(BaseModel):
    filename: str
    content_type: str  # e.g. "video/mp4"


class UploadUrlResponse(BaseModel):
    upload_url: str
    gcs_uri: str
    source_video_id: UUID
    expires_in_seconds: int = 900


class JobCreate(BaseModel):
    source_video_id: UUID
    target_languages: list[str]  # creates 1 Job per language


class AttachVideoRequest(BaseModel):
    """Body of POST /jobs/{id}/attach-video (US5) — the video itself is
    uploaded the same way as any other source video, via the existing
    POST /projects/{id}/upload-url; this just links an already-uploaded
    SourceVideo to an already-dubbed script-first job."""

    source_video_id: UUID


# --- Script-First Job Creation (specs/003-script-first-ingestion) ---


class VoiceOption(BaseModel):
    """1 of the 12 prebuilt TTS voices, for a human to pick from directly —
    see GET /characters/voice-options, gemini_client.py::list_prebuilt_voices."""

    voice_name: str
    description: str


class VoiceAssignment(BaseModel):
    """A human-picked prebuilt voice for one character in a script-first job
    — no audio sample, no Gemini classification call (see
    GET /characters/voice-options, PUT /characters/{id}/voice). Mirrors the
    manual dubbing process's own approach for this feature's equivalent step,
    just without the audio-based automatic matching that normally applies
    when a real video exists (see VoiceProfilingAgent)."""

    voice_name: str = Field(min_length=1)

    @field_validator("voice_name")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        # Field(min_length=1) alone accepts a whitespace-only string (pydantic
        # does not strip before counting length) — found via
        # gemini-prompt-reviewer-adjacent review, 2026-10-10.
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank/whitespace-only")
        return stripped


class ScriptLineImport(BaseModel):
    """One row of an uploaded script — becomes exactly one DialogueLine, with
    no merging/splitting (mirrors the manual dubbing process's own "preserve
    100% of rows" rule, see docs/manual-dubbing-process.docx)."""

    segment_code: str = Field(min_length=1)
    character_name: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("segment_code", "character_name", "text")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        # Same gap as VoiceAssignment.voice_name above: Field(min_length=1)
        # alone would accept e.g. segment_code=" " as a valid, non-empty,
        # non-duplicate identifier — stripping here means a script row that's
        # blank after trimming is correctly rejected, not silently imported as
        # a line with invisible "content".
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank/whitespace-only")
        return stripped


class ScriptJobCreate(BaseModel):
    """Body of POST /projects/{project_id}/script-jobs — creates 1 Job per
    target language, with no source video required yet (US1 in spec.md).
    Every distinct character_name found in `lines` must have a voice
    assignment (US2) — validated here, at the API boundary, rather than left
    for the endpoint to discover line by line."""

    lines: list[ScriptLineImport] = Field(min_length=1)
    target_languages: list[str] = Field(min_length=1)
    character_voice_assignments: dict[str, VoiceAssignment]

    @model_validator(mode="after")
    def _check_script(self) -> "ScriptJobCreate":
        segment_codes = [line.segment_code for line in self.lines]
        if len(segment_codes) != len(set(segment_codes)):
            raise ValueError("Duplicate segment_code in script")
        character_names = {line.character_name for line in self.lines}
        missing = character_names - set(self.character_voice_assignments.keys())
        if missing:
            raise ValueError(f"Missing voice assignment for character(s): {sorted(missing)}")
        return self


class SourceVideoOut(BaseModel):
    id: UUID
    gcs_uri: str
    duration_seconds: int | None
    status: str

    class Config:
        from_attributes = True


class SignedReadUrlResponse(BaseModel):
    """Used by AudioPlayer.tsx to play dubbed audio/voice samples — the GCS
    bucket is not public-read so https://storage.googleapis.com/... cannot be
    used directly."""

    url: str
    expires_in_seconds: int = 3600


class JobOut(BaseModel):
    id: UUID
    # Exposed so the frontend can call POST /projects/{project_id}/upload-url
    # for the "Attach Video" flow (specs/003-script-first-ingestion/US5)
    # without already knowing which project a job belongs to.
    project_id: UUID
    # Nullable since specs/003-script-first-ingestion — None means a
    # script-first job with no video attached yet (see
    # api/db/models.py::Job.source_video_id).
    source_video_id: UUID | None
    target_language: str
    current_stage: str
    status: str
    muxed_video_gcs_uri: str | None
    # The Video Editor export (specs/002-video-editor) — null if no export
    # has happened yet. Separate from muxed_video_gcs_uri (the original AI
    # output, never overwritten, see US5 in spec.md).
    exported_video_gcs_uri: str | None = None

    class Config:
        from_attributes = True


class DialogueLineOut(BaseModel):
    id: UUID
    character_id: UUID | None
    start_time: float
    end_time: float
    original_text: str
    translated_text: str | None
    is_song: bool
    safety_status: str
    safety_reasoning: str | None
    dubbed_audio_gcs_uri: str | None
    # Only set for a line imported from a script-first job's uploaded script
    # (specs/003-script-first-ingestion) — None for a line produced by
    # transcription. Lets the frontend label/name an exported dubbed-audio
    # file traceably back to its row in the original script (FR-008).
    segment_code: str | None = None

    class Config:
        from_attributes = True


class DialogueLineUpdate(BaseModel):
    """All fields are optional — you can edit just the text, just change the
    voice (character_id), just override safety_status, or any combination at
    once (the endpoint requires at least one field, see
    api/routers/jobs.py::update_dialogue_line).

    safety_status: a manual reviewer override — DubbingAgent only ever dubs a
    line when safety_status == "ok" (see dubbing_agent.py), and the pipeline
    resume path used after an edit deliberately skips re-running SafetyAgent
    (see update_dialogue_line docstring), so a "flagged" line's status never
    changes on its own — a real UX gap found via feedback: editing the
    translated text of a flagged line to fix the issue had NO way to actually
    unblock dubbing afterward. Only "ok"/"flagged" are accepted here (not
    "pending" — that's an initial AI-only state, not a human review
    outcome)."""

    translated_text: str | None = None
    character_id: UUID | None = None
    safety_status: Literal["ok", "flagged"] | None = None


class VocabularyItemOut(BaseModel):
    id: UUID
    term: str
    meaning: str
    example_sentence: str

    class Config:
        from_attributes = True


class QuizItemOut(BaseModel):
    id: UUID
    question: str
    answer: str

    class Config:
        from_attributes = True


class EducationalContentOut(BaseModel):
    """Body of GET /jobs/{id}/educational-content — Agent 7's output
    (worker/agents/educational_agent.py), persisted to vocabulary_items/
    quiz_items. Runs off the critical path, so both lists may still be empty
    once the job reaches ready_for_review (no separate "still generating"
    status is tracked for it, see docs/agent-pipeline.md)."""

    vocabulary: list[VocabularyItemOut]
    quiz: list[QuizItemOut]


class CharacterOut(BaseModel):
    id: UUID
    name: str
    voice_sample_gcs_uri: str | None
    voice_name: str | None = None
    voice_id: str | None = None

    class Config:
        from_attributes = True


class CharacterUpdate(BaseModel):
    name: str


class CharacterCreate(BaseModel):
    """Create a character manually along with a self-uploaded sample voice —
    unlike characters automatically detected from the video by Agent 2
    (VoiceProfilingAgent), used when you want to add/change a voice without
    depending on the automatic diarization result."""

    project_id: UUID
    name: str


class VoiceSampleUploadUrlResponse(BaseModel):
    upload_url: str
    gcs_uri: str
    expires_in_seconds: int = 900


# --- Video Editor (specs/002-video-editor) ---
# Validates the content of jobs.timeline_edits (JSONB) at the API boundary —
# see Clarifications #1 in specs/002-video-editor/spec.md: the current
# track/clip snapshot is stored in a single JSONB column instead of a separate
# relational table. NOTE: the DB default value is `{}` (the job's editor has
# never been opened) — this is an "uninitialized" sentinel and must NOT be
# parsed directly as a TimelineState (the fields below are required); the
# caller (router) must build a default timeline from dialogue_lines when it
# encounters `{}` instead of validating it as-is.


class Clip(BaseModel):
    """1 segment on 1 track. `dialogue_line_id` is required for a dialogue
    clip (track type="dialogue"), and forbidden for a video/original-audio
    clip — see the validator on Track."""

    id: str
    dialogue_line_id: UUID | None = None
    timeline_start: float = Field(ge=0.0)
    source_start: float = Field(ge=0.0)
    source_end: float
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    fade_in_seconds: float = Field(default=0.0, ge=0.0)
    fade_out_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _check_ranges(self) -> "Clip":
        # Field(ge=/le=) already covers the single-field constraints (and
        # shows up in the OpenAPI schema); only one CROSS-field constraint is
        # left that needs model_validator.
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class Track(BaseModel):
    """1 lane on the timeline. `character_id` is required when
    type="dialogue" (1 dialogue/character track), and forbidden for the
    other 2 track types — see Terminology in spec.md."""

    id: str
    type: Literal["video", "audio_original", "dialogue"]
    character_id: UUID | None = None
    muted: bool = False
    solo: bool = False
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    clips: list[Clip] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_track(self) -> "Track":
        if self.type == "dialogue" and self.character_id is None:
            raise ValueError("Track type=dialogue requires character_id")
        if self.type != "dialogue" and self.character_id is not None:
            raise ValueError("character_id is only valid for track type=dialogue")

        clip_ids = [clip.id for clip in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError(f"Duplicate clip id in track {self.id}")

        for clip in self.clips:
            is_dialogue_clip = clip.dialogue_line_id is not None
            if self.type == "dialogue" and not is_dialogue_clip:
                raise ValueError("A clip in a dialogue track requires dialogue_line_id")
            if self.type != "dialogue" and is_dialogue_clip:
                raise ValueError("dialogue_line_id is only valid for a clip in a dialogue track")
        # US3: "Clips cannot be dragged to overlap within the same track" —
        # blocked right here at the boundary instead of relying on the
        # frontend to enforce it, since autosave writes this field directly.
        ordered = sorted(self.clips, key=lambda c: c.timeline_start)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            prev_end = prev.timeline_start + (prev.source_end - prev.source_start)
            if nxt.timeline_start < prev_end:
                raise ValueError(f"Overlapping clips in track {self.id}")
        return self


class TimelineState(BaseModel):
    """The entire content of `jobs.timeline_edits`. `schema_version` guards
    against future JSON structure changes (see Technical Risks in
    specs/002-video-editor/plan.md)."""

    schema_version: int = 1
    duration_seconds: float = Field(gt=0.0)
    tracks: list[Track]

    @model_validator(mode="after")
    def _check_timeline(self) -> "TimelineState":
        track_ids = [t.id for t in self.tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("Duplicate track id")
        # A clip that falls outside the timeline's total duration would have
        # nothing to render at export time (editor_export_service.py) —
        # blocked right here instead of surfacing as a truncated video at
        # export time.
        for track in self.tracks:
            for clip in track.clips:
                clip_end = clip.timeline_start + (clip.source_end - clip.source_start)
                if clip_end > self.duration_seconds:
                    raise ValueError(f"Clip {clip.id} in track {track.id} exceeds duration_seconds")
        return self


class TimelineUpdate(BaseModel):
    """Body of PATCH /jobs/{id}/timeline — the client always sends the entire
    new snapshot (not a partial diff), debounced autosave overwrites
    `timeline_edits` wholesale."""

    timeline: TimelineState
