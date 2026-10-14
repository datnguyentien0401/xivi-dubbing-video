"""Endpoints for managing projects + generating video upload signed URLs.

Per the fastapi-agent-endpoint skill: this router does NOT call Gemini/ffmpeg,
it only does CRUD + signed URL generation.
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.firebase_auth import CurrentUser, get_current_user
from api.core.local_pubsub_client import schedule_local_job_created
from api.core.ownership import get_owned_project
from api.core.pubsub_client import publish_job_created
from api.db.models import Character, DialogueLine, Job, Project, SourceVideo, VocabularyItem
from api.db.session import get_session
from api.schemas.schemas import (
    JobOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ScriptJobCreate,
    SourceVideoOut,
    UploadUrlRequest,
    UploadUrlResponse,
)
from worker.services.storage import build_storage_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

# Shared client instance, avoids recreating storage.Client() on every request
# (see worker/services/gcs_client.py — every method is already wrapped async
# via to_thread). GCSClient or LocalStorageClient depending on
# settings.use_local_storage — see worker/services/storage.py.
_storage = build_storage_client()


@router.post("", response_model=ProjectOut)
async def create_project(
    payload: ProjectCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    project = Project(user_id=user.id, name=payload.name, source_language=payload.source_language)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{project_id}/source-videos", response_model=list[SourceVideoOut])
async def list_source_videos(
    project_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Used by the Project Detail page — see frontend/src/pages/ProjectDetail.tsx."""
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user
    result = await session.execute(select(SourceVideo).where(SourceVideo.project_id == project_id))
    return result.scalars().all()


@router.get("/{project_id}/jobs", response_model=list[JobOut])
async def list_project_jobs(
    project_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """All jobs (every source video, every target language) belonging to the
    project — used by the Project Detail page to show each translation's progress."""
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user
    # Reads Job.project_id directly (not by joining through source_video_id)
    # since specs/003-script-first-ingestion — a script-first job has no
    # source_video_id, so the old join would have silently excluded it from
    # this list.
    result = await session.execute(select(Job).where(Job.project_id == project_id))
    return result.scalars().all()


@router.post(
    "/{project_id}/script-jobs", response_model=list[JobOut], status_code=status.HTTP_202_ACCEPTED
)
async def create_script_jobs(
    project_id: uuid.UUID,
    payload: ScriptJobCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Create one job per target language directly from an uploaded script —
    no source video required yet (specs/003-script-first-ingestion, US1).
    Mirrors create_jobs in jobs.py, just starting from a locked script
    instead of an already-recorded video; duplicate/missing-assignment
    validation already happened at the schema boundary (ScriptJobCreate).

    Placed in this router (not jobs.py, despite plan.md's original file
    estimate) to match the path prefix /projects/{project_id}/... — the same
    reasoning list_project_jobs above already follows."""
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user

    # Resolve each character name to a Character row: reuse an existing one
    # in this project if the name already exists (US2 — "default to a
    # character's prior voice choice from an earlier job in the same
    # project" — keeps the series' voice consistent instead of silently
    # overwriting an already-established voice), otherwise create one from
    # the manual assignment.
    existing_characters = (
        (await session.execute(select(Character).where(Character.project_id == project_id)))
        .scalars()
        .all()
    )
    characters_by_name = {c.name: c for c in existing_characters}
    for character_name in {line.character_name for line in payload.lines}:
        if character_name in characters_by_name:
            # A character with this name already exists in the project —
            # keep its established voice (US2) rather than silently
            # overwriting it. Logged (not just silently dropped) to match the
            # project's established convention for any boundary where data is
            # altered/discarded rather than applied as given (see
            # ingestion_agent.py's duration backstop) — otherwise a user who
            # explicitly picked a different voice for this script-jobs call
            # has no way to know why it didn't take effect.
            existing = characters_by_name[character_name]
            assignment = payload.character_voice_assignments.get(character_name)
            if assignment is not None and assignment.voice_name != existing.voice_name:
                logger.warning(
                    "project %s: ignoring conflicting voice assignment %r for "
                    "existing character %r (keeping %r)",
                    project_id,
                    assignment.voice_name,
                    character_name,
                    existing.voice_name,
                )
            continue
        assignment = payload.character_voice_assignments[character_name]
        character = Character(
            project_id=project_id,
            name=character_name,
            # speaker_tag_original doesn't apply to a manually created
            # character — see create_character above (same "manual_" prefix
            # convention, so it never collides with a real "speaker_N" tag
            # from transcription).
            speaker_tag_original=f"manual_{uuid.uuid4()}",
            voice_profile_metadata={
                "voice_name": assignment.voice_name,
                "manually_assigned": True,
            },
        )
        session.add(character)
        characters_by_name[character_name] = character
    await session.flush()  # new Character rows need an id before use below

    jobs = [
        Job(project_id=project_id, source_video_id=None, target_language=lang)
        for lang in payload.target_languages
    ]
    session.add_all(jobs)
    await session.flush()  # job.id needed below

    # Sequential placeholder timestamps (1 second per line) — overwritten
    # with real values derived from the English benchmark audio once
    # ScriptBenchmarkAgent runs (see the "transcribed" pipeline stage); kept
    # here only so DialogueLine's NOT NULL start_time/end_time and every
    # downstream `ORDER BY start_time` query stay valid in the meantime.
    for job in jobs:
        for index, line in enumerate(payload.lines):
            character = characters_by_name[line.character_name]
            session.add(
                DialogueLine(
                    job_id=job.id,
                    character_id=character.id,
                    segment_code=line.segment_code,
                    start_time=float(index),
                    end_time=float(index + 1),
                    original_text=line.text,
                )
            )
    await session.commit()

    for job in jobs:
        await session.refresh(job)
        if settings.use_local_pubsub:
            schedule_local_job_created(background_tasks, str(job.id))
        else:
            publish_job_created(str(job.id))
    return jobs


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Rename a project — currently only `name` is editable, `source_language`
    cannot be changed yet (changing the source language after source_video/
    dialogue_lines already tied to that language exist would corrupt all the
    already-translated data, which is out of scope for this simple rename
    feature)."""
    project = await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user
    project.name = payload.name
    await session.commit()
    await session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a project + all its related data in the DB.

    The models don't declare cascade="all, delete-orphan" (relationship) or
    ON DELETE CASCADE (DB-level FK), so deletion must follow the correct FK
    order manually, otherwise it will hit a foreign key violation.

    TODO: doesn't delete the GCS files yet (original video, dubbed audio,
    muxed video) — blobs remain in the bucket after the project is deleted.
    Need to add a delete/delete_prefix method to StorageClient
    (worker/services/storage.py) then clean up via a separate asynchronous
    Pub/Sub job (per AGENTS.md principle #4 — don't block this request to
    call GCS deletion), not done in this pass.
    """
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user

    video_ids = (
        (await session.execute(select(SourceVideo.id).where(SourceVideo.project_id == project_id)))
        .scalars()
        .all()
    )
    # Reads Job.project_id directly (not by joining through source_video_id)
    # since specs/003-script-first-ingestion — a script-first job has no
    # source_video_id at all, so the old join-through-video approach would
    # silently skip it here, leaving it to hit a foreign key violation against
    # the project row being deleted below.
    job_ids = (
        (await session.execute(select(Job.id).where(Job.project_id == project_id))).scalars().all()
    )

    if job_ids:
        await session.execute(delete(DialogueLine).where(DialogueLine.job_id.in_(job_ids)))
        await session.execute(delete(VocabularyItem).where(VocabularyItem.job_id.in_(job_ids)))
        await session.execute(delete(Job).where(Job.id.in_(job_ids)))
    if video_ids:
        await session.execute(delete(SourceVideo).where(SourceVideo.id.in_(video_ids)))
    await session.execute(delete(Character).where(Character.project_id == project_id))
    await session.execute(delete(Project).where(Project.id == project_id))
    await session.commit()


@router.post("/{project_id}/upload-url", response_model=UploadUrlResponse)
async def create_upload_url(
    project_id: uuid.UUID,
    payload: UploadUrlRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate a signed URL so the frontend can upload directly to GCS (bypassing the backend)."""
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user
    blob_name = f"projects/{project_id}/source/{uuid.uuid4()}-{payload.filename}"
    upload_url = await _storage.generate_signed_url(blob_name, content_type=payload.content_type)
    gcs_uri = _storage.blob_uri(blob_name)

    video = SourceVideo(project_id=project_id, gcs_uri=gcs_uri, status="pending_upload")
    session.add(video)
    await session.commit()

    return UploadUrlResponse(upload_url=upload_url, gcs_uri=gcs_uri, source_video_id=video.id)
