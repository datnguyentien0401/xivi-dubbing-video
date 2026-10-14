"""Checks resource ownership against the current user (anti-IDOR).

Every endpoint that accesses a resource by id (job/dialogue-line/character/
source-video) MUST go through the functions here instead of `session.get()`
directly — `session.get()` only checks that the resource exists, it does not
check whether that resource belongs to `current_user` (this used to be an IDOR
vulnerability: any logged-in user could read/edit/approve another user's job).

Always returns 404 (not 403) when the resource exists but doesn't belong to the
user — this avoids leaking to a non-owner whether that resource exists at all.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Character, DialogueLine, Job, Project, SourceVideo


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")


async def get_owned_project(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user_id:
        raise _not_found()
    return project


async def get_owned_source_video(
    session: AsyncSession, source_video_id: uuid.UUID, user_id: uuid.UUID
) -> SourceVideo:
    result = await session.execute(
        select(SourceVideo)
        .join(Project, Project.id == SourceVideo.project_id)
        .where(SourceVideo.id == source_video_id, Project.user_id == user_id)
    )
    video = result.scalar_one_or_none()
    if video is None:
        raise _not_found()
    return video


async def get_owned_job(session: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
    # Joins through Job.project_id directly (not via source_video_id ->
    # SourceVideo.project_id) since specs/003-script-first-ingestion — a
    # script-first job has no source_video_id, so the old join would have
    # returned no row (an inner join through a NULL FK matches nothing),
    # incorrectly 404-ing the job's own legitimate owner on nearly every
    # job-scoped endpoint (this function gates almost all of them).
    result = await session.execute(
        select(Job)
        .join(Project, Project.id == Job.project_id)
        .where(Job.id == job_id, Project.user_id == user_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise _not_found()
    return job


async def get_owned_dialogue_line(
    session: AsyncSession, line_id: uuid.UUID, user_id: uuid.UUID
) -> DialogueLine:
    # See get_owned_job above for why this joins through Job.project_id
    # directly rather than via source_video_id.
    result = await session.execute(
        select(DialogueLine)
        .join(Job, Job.id == DialogueLine.job_id)
        .join(Project, Project.id == Job.project_id)
        .where(DialogueLine.id == line_id, Project.user_id == user_id)
    )
    line = result.scalar_one_or_none()
    if line is None:
        raise _not_found()
    return line


async def get_owned_character(
    session: AsyncSession, character_id: uuid.UUID, user_id: uuid.UUID
) -> Character:
    result = await session.execute(
        select(Character)
        .join(Project, Project.id == Character.project_id)
        .where(Character.id == character_id, Project.user_id == user_id)
    )
    character = result.scalar_one_or_none()
    if character is None:
        raise _not_found()
    return character
