"""PipelineContext — the single object passed through each agent in the
pipeline.

Each agent receives the context, modifies/adds the part it's responsible for,
and returns the context — no agent is allowed to mutate a field outside its
own responsibility (see the gemini-pipeline-agent skill).
"""

from pydantic import BaseModel, Field


class DialogueLine(BaseModel):
    id: str
    character_id: str | None = None
    speaker_tag: str | None = None  # assigned by Agent 1, before an official character_id exists
    start_time: float
    end_time: float
    original_text: str
    translated_text: str | None = None
    is_song: bool = False
    safety_status: str = "pending"  # pending | ok | flagged
    safety_reasoning: str | None = None
    dubbed_audio_gcs_uri: str | None = None


class Character(BaseModel):
    id: str
    name: str
    speaker_tag_original: str
    voice_sample_gcs_uri: str | None = None
    voice_profile_metadata: dict = Field(default_factory=dict)


class PipelineContext(BaseModel):
    job_id: str
    project_id: str
    # None for a script-first job with no video attached yet
    # (specs/003-script-first-ingestion) — see worker/pipeline.py, which
    # picks ScriptBenchmarkAgent instead of IngestionAgent for the
    # "transcribed" stage whenever this is None, and skips the "muxed" stage
    # entirely until a real video is later attached.
    source_video_gcs_uri: str | None
    source_language: str
    target_language: str
    current_stage: str = "queued"
    dialogue_lines: list[DialogueLine] = Field(default_factory=list)
    characters: list[Character] = Field(default_factory=list)
    muxed_video_gcs_uri: str | None = None
