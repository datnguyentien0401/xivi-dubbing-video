"""Regression test for a fixed contract bug: POST /projects/{id}/upload-url
used to create a SourceVideo but didn't return `source_video_id` in the
response, leaving the frontend with no way to get that id to subsequently call
POST /jobs (see frontend/src/pages/NewProject.tsx). Needs DATABASE_URL pointed
at a real Postgres (same reason as api/core/test_ownership.py — the logic
involves SQL, which a mock wouldn't catch).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/routers/test_projects.py
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# CurrentUser/projects_router import api.core.config (Settings) at their
# module level — Settings() raises if environment variables are missing
# (GOOGLE_CLOUD_PROJECT, REDIS_URL, GCS_BUCKET_NAME, FIREBASE_PROJECT_ID),
# which has nothing to do with whether this test has DATABASE_URL set. Import
# those two LOCALLY inside the test function (not here) so that skipping via
# the pytestmark below actually avoids crashing during pytest collection when
# running plain `pytest` without those variables set.
from api.db.models import Project, SourceVideo, User
from api.schemas.schemas import ProjectUpdate, UploadUrlRequest

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the SQL join (see docstring)",
)


@pytest_asyncio.fixture
async def owner_and_project(session: AsyncSession):
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    return owner, project


async def test_list_projects_sorts_newest_first(session: AsyncSession):
    """Regression test — list_projects previously had no ORDER BY at all, so
    the display order on the Dashboard was whatever Postgres happened to
    return (not guaranteed to be insertion order), meaning a newly created
    project could appear anywhere in the list instead of at the top."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    now = datetime.utcnow()
    oldest = Project(
        user_id=owner.id, name="oldest", source_language="vi", created_at=now - timedelta(days=2)
    )
    newest = Project(user_id=owner.id, name="newest", source_language="vi", created_at=now)
    middle = Project(
        user_id=owner.id, name="middle", source_language="vi", created_at=now - timedelta(days=1)
    )
    # Added out of chronological order on purpose, so a passing test can only
    # be explained by an actual ORDER BY, not by coincidentally matching
    # insertion order.
    session.add_all([oldest, newest, middle])
    await session.flush()

    response = await projects_router.list_projects(
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        session=session,
    )

    assert [p.name for p in response] == ["newest", "middle", "oldest"]


async def test_create_upload_url_response_contains_the_actual_source_video_id(
    session: AsyncSession, owner_and_project, monkeypatch
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    owner, project = owner_and_project

    async def fake_generate_signed_url(*args, **kwargs):
        return "https://fake-signed-url.test/"

    monkeypatch.setattr(projects_router._storage, "generate_signed_url", fake_generate_signed_url)

    response = await projects_router.create_upload_url(
        project_id=project.id,
        payload=UploadUrlRequest(filename="ep1.mp4", content_type="video/mp4"),
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        session=session,
    )

    # source_video_id in the response must match the actual SourceVideo
    # created in the DB for this project — this is exactly the fixed bug
    # (previously this field didn't exist in the response at all).
    result = await session.execute(select(SourceVideo).where(SourceVideo.project_id == project.id))
    created_video = result.scalar_one()
    assert response.source_video_id == created_video.id


async def test_update_project_renames_and_persists(session: AsyncSession, owner_and_project):
    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    owner, project = owner_and_project

    response = await projects_router.update_project(
        project_id=project.id,
        payload=ProjectUpdate(name="Renamed project"),
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        session=session,
    )

    assert response.name == "Renamed project"
    # Must actually be written to the DB (commit), not just changed on the
    # in-memory object — read it back through a NEW session so it isn't
    # masked by the current session's identity map.
    result = await session.execute(select(Project).where(Project.id == project.id))
    assert result.scalar_one().name == "Renamed project"


async def test_update_project_404_when_not_owned_by_current_user(
    session: AsyncSession, owner_and_project
):
    """Regression test for IDOR (see docstring in api/core/ownership.py) — a
    user who does NOT own the project must get a 404, and must not be allowed
    to rename someone else's project even if they know the correct project_id."""
    from fastapi import HTTPException

    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router

    _owner, project = owner_and_project
    other_user = User(
        firebase_uid=f"other-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@test.local"
    )
    session.add(other_user)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await projects_router.update_project(
            project_id=project.id,
            payload=ProjectUpdate(name="Hijacked name"),
            user=CurrentUser(
                id=other_user.id, firebase_uid=other_user.firebase_uid, email=other_user.email
            ),
            session=session,
        )
    assert exc_info.value.status_code == 404


async def test_create_script_jobs_creates_one_job_per_language_with_dialogue_lines(
    session: AsyncSession, owner_and_project, monkeypatch
):
    """specs/003-script-first-ingestion US1 — a job can be created directly
    from a script, no source video required."""
    from fastapi import BackgroundTasks

    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Character, DialogueLine
    from api.routers import projects as projects_router
    from api.schemas.schemas import ScriptJobCreate

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, project = owner_and_project
    background_tasks = BackgroundTasks()

    jobs = await projects_router.create_script_jobs(
        project_id=project.id,
        payload=ScriptJobCreate(
            lines=[
                {"segment_code": "001", "character_name": "Cat", "text": "Hello!"},
                {"segment_code": "002", "character_name": "Dog", "text": "Hi there!"},
            ],
            target_languages=["id", "th"],
            character_voice_assignments={
                "Cat": {"voice_name": "Puck"},
                "Dog": {"voice_name": "Kore"},
            },
        ),
        background_tasks=background_tasks,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert len(jobs) == 2  # 1 per target language
    assert {job.target_language for job in jobs} == {"id", "th"}
    assert all(job.source_video_id is None for job in jobs)
    assert len(background_tasks.tasks) == 2  # 1 scheduled job-created task per job

    characters = (
        (await session.execute(select(Character).where(Character.project_id == project.id)))
        .scalars()
        .all()
    )
    assert {c.name for c in characters} == {"Cat", "Dog"}
    assert {c.voice_name for c in characters} == {"Puck", "Kore"}

    for job in jobs:
        lines = (
            (await session.execute(select(DialogueLine).where(DialogueLine.job_id == job.id)))
            .scalars()
            .all()
        )
        assert {line.segment_code for line in lines} == {"001", "002"}
        assert all(line.character_id is not None for line in lines)


async def test_create_script_jobs_reuses_an_existing_characters_voice(
    session: AsyncSession, owner_and_project, caplog
):
    """specs/003-script-first-ingestion US2 — "default to a character's prior
    voice choice from an earlier job in the same project" — must not let a
    NEW script import silently overwrite a voice already established for a
    character with the same name, even if the new payload's assignment
    disagrees. The conflict must be logged (found via code-reviewer, review
    2026-10-10) — a user who explicitly picked a different voice otherwise
    has no way to know why it didn't take effect, inconsistent with how
    every other silent-alter boundary in this codebase logs a warning (see
    ingestion_agent.py's duration backstop)."""
    from fastapi import BackgroundTasks

    from api.core.firebase_auth import CurrentUser
    from api.db.models import Character
    from api.routers import projects as projects_router
    from api.schemas.schemas import ScriptJobCreate

    owner, project = owner_and_project
    existing_cat = Character(
        project_id=project.id,
        name="Cat",
        speaker_tag_original=f"manual_{uuid.uuid4()}",
        voice_profile_metadata={"voice_name": "Autonoe", "manually_assigned": True},
    )
    session.add(existing_cat)
    await session.flush()

    await projects_router.create_script_jobs(
        project_id=project.id,
        payload=ScriptJobCreate(
            lines=[{"segment_code": "001", "character_name": "Cat", "text": "Hello!"}],
            target_languages=["id"],
            # Deliberately conflicting voice_name — must be ignored since
            # "Cat" already exists in this project.
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        ),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    characters = (
        (await session.execute(select(Character).where(Character.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(characters) == 1  # no duplicate "Cat" character created
    assert characters[0].voice_name == "Autonoe"  # untouched, not overwritten by "Puck"
    assert "ignoring conflicting voice assignment" in caplog.text


async def test_create_script_jobs_placeholder_timestamps_match_line_order(
    session: AsyncSession, owner_and_project
):
    """The placeholder sequential timestamps (overwritten later by
    ScriptBenchmarkAgent) must correspond to each line's position in the
    original script, not just exist with no particular order — this is what
    every downstream `ORDER BY start_time` query relies on to reconstruct the
    script's original line order."""
    from fastapi import BackgroundTasks

    from api.core.firebase_auth import CurrentUser
    from api.db.models import DialogueLine
    from api.routers import projects as projects_router
    from api.schemas.schemas import ScriptJobCreate

    owner, project = owner_and_project

    jobs = await projects_router.create_script_jobs(
        project_id=project.id,
        payload=ScriptJobCreate(
            lines=[
                {"segment_code": "001", "character_name": "Cat", "text": "One"},
                {"segment_code": "002", "character_name": "Cat", "text": "Two"},
                {"segment_code": "003", "character_name": "Cat", "text": "Three"},
            ],
            target_languages=["id"],
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        ),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    lines = (
        (
            await session.execute(
                select(DialogueLine)
                .where(DialogueLine.job_id == jobs[0].id)
                .order_by(DialogueLine.start_time)
            )
        )
        .scalars()
        .all()
    )
    assert [line.segment_code for line in lines] == ["001", "002", "003"]
    assert [(line.start_time, line.end_time) for line in lines] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
    ]


async def test_create_script_jobs_404_when_project_not_owned(
    session: AsyncSession, owner_and_project
):
    from fastapi import BackgroundTasks, HTTPException

    from api.core.firebase_auth import CurrentUser
    from api.routers import projects as projects_router
    from api.schemas.schemas import ScriptJobCreate

    _owner, project = owner_and_project
    other_user = User(
        firebase_uid=f"other-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@test.local"
    )
    session.add(other_user)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await projects_router.create_script_jobs(
            project_id=project.id,
            payload=ScriptJobCreate(
                lines=[{"segment_code": "001", "character_name": "Cat", "text": "Hello!"}],
                target_languages=["id"],
                character_voice_assignments={"Cat": {"voice_name": "Puck"}},
            ),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(
                id=other_user.id, firebase_uid=other_user.firebase_uid, email=other_user.email
            ),
        )
    assert exc_info.value.status_code == 404
