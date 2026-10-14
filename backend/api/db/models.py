"""SQLAlchemy models — match docs/database-schema.md 1-to-1.

Run `alembic revision --autogenerate -m "init schema"` after editing this
file to generate the corresponding migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_col():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    """The internal id is its own uuid (per the database-schema.md ERD);
    firebase_uid maps to the Firebase Auth uid — does NOT use the firebase uid
    directly as the PK since that's an arbitrary Firebase string, not a uuid
    (see api/core/firebase_auth.py)."""

    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_col()
    firebase_uid: Mapped[str] = mapped_column(String, unique=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="user")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _uuid_col()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String)
    source_language: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="projects")
    videos: Mapped[list["SourceVideo"]] = relationship(back_populates="project")
    characters: Mapped[list["Character"]] = relationship(back_populates="project")
    jobs: Mapped[list["Job"]] = relationship(back_populates="project")


class SourceVideo(Base):
    __tablename__ = "source_videos"
    id: Mapped[uuid.UUID] = _uuid_col()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    gcs_uri: Mapped[str] = mapped_column(String)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String, default="uploaded")

    project: Mapped[Project] = relationship(back_populates="videos")
    jobs: Mapped[list["Job"]] = relationship(back_populates="source_video")


class Character(Base):
    __tablename__ = "characters"
    __table_args__ = (Index("idx_characters_project", "project_id"),)
    id: Mapped[uuid.UUID] = _uuid_col()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String)
    speaker_tag_original: Mapped[str] = mapped_column(String)
    voice_sample_gcs_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    voice_profile_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    project: Mapped[Project] = relationship(back_populates="characters")

    @property
    def voice_name(self) -> str | None:
        """The selected prebuilt voice (1 of 12 fixed voices, used as a
        fallback when there's no voice_id — see
        worker/agents/voice_profiling_agent.py) — exposed as its own field on
        CharacterOut instead of forcing the frontend to dig into
        voice_profile_metadata (a free-form dict, not a stable contract)."""
        return (self.voice_profile_metadata or {}).get("voice_name")

    @property
    def voice_id(self) -> str | None:
        """A custom synthesized voice via Voice Design (matches the original
        voice's characteristics far better than voice_name's 12 prebuilt
        voices, see gemini_client.py::design_voice) — None if the Voice
        Design step failed, in which case the character is using the
        voice_name fallback."""
        return (self.voice_profile_metadata or {}).get("voice_id")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_source_video", "source_video_id"),
        Index("idx_jobs_project", "project_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_col()
    # Direct FK to Project — see specs/003-script-first-ingestion/plan.md: a
    # script-first job has no source_video_id (see below), so a job can no
    # longer rely on joining through SourceVideo to find its Project. Every
    # job (video-first or script-first) sets this directly at creation time.
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    # Nullable since specs/003-script-first-ingestion: NULL means a
    # script-first job that has not had a real video attached yet (see
    # POST /jobs/{id}/attach-video) — source_video_id IS NULL is the single
    # source of truth other code branches on (e.g. approve_job, the mux
    # pipeline stage), deliberately not a separate boolean column.
    source_video_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_videos.id"), nullable=True
    )
    target_language: Mapped[str] = mapped_column(String)
    current_stage: Mapped[str] = mapped_column(String, default="queued")
    status: Mapped[str] = mapped_column(String, default="queued")
    muxed_video_gcs_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    # Timeline/track/clip snapshot for the Video Editor (specs/002-video-editor)
    # — see Clarifications #1 in spec.md: a single JSONB column instead of a
    # separate relational table. Validated via TimelineState
    # (api/schemas/schemas.py) at the API boundary, not validated at the DB layer.
    timeline_edits: Mapped[dict] = mapped_column(JSONB, default=dict)
    # The Video Editor export — SEPARATE from muxed_video_gcs_uri (the original
    # AI output, never overwritten, see US5 in specs/002-video-editor/spec.md).
    # The video/share-url/approve read endpoints prefer this column, falling
    # back to muxed_video_gcs_uri if NULL (no export yet).
    exported_video_gcs_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
    source_video: Mapped[SourceVideo | None] = relationship(back_populates="jobs")
    dialogue_lines: Mapped[list["DialogueLine"]] = relationship(back_populates="job")
    vocabulary_items: Mapped[list["VocabularyItem"]] = relationship(back_populates="job")


class DialogueLine(Base):
    __tablename__ = "dialogue_lines"
    __table_args__ = (
        Index("idx_dialogue_lines_job_id", "job_id"),
        Index("idx_dialogue_lines_safety_status", "job_id", "safety_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_col()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True
    )
    # Only set for a line imported from a script-first job's uploaded script
    # (specs/003-script-first-ingestion) — NULL for a line produced by
    # transcription (there is no equivalent identifier in that flow). Lets a
    # human editor match an exported dubbed-audio file back to its row in the
    # original script.
    segment_code: Mapped[str | None] = mapped_column(String, nullable=True)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    original_text: Mapped[str] = mapped_column(Text)
    translated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_song: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_status: Mapped[str] = mapped_column(String, default="pending")
    safety_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    dubbed_audio_gcs_uri: Mapped[str | None] = mapped_column(String, nullable=True)

    job: Mapped[Job] = relationship(back_populates="dialogue_lines")


class VocabularyItem(Base):
    __tablename__ = "vocabulary_items"
    id: Mapped[uuid.UUID] = _uuid_col()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    term: Mapped[str] = mapped_column(String)
    meaning: Mapped[str] = mapped_column(String)
    example_sentence: Mapped[str] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="vocabulary_items")


class CulturalSafetyRule(Base):
    __tablename__ = "cultural_safety_rules"
    id: Mapped[uuid.UUID] = _uuid_col()
    target_market: Mapped[str] = mapped_column(String)
    rule_description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String, default="medium")
