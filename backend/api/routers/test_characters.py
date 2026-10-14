"""Regression test for GET /characters/{id}/voice-sample-url — same reason as
api/routers/test_jobs.py (the GCS bucket is not public-read, AudioPlayer.tsx
needs a real signed URL from the backend rather than constructing a
storage.googleapis.com URL itself).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/routers/test_characters.py
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Character, Project, User

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the SQL join (see docstring)",
)


@pytest_asyncio.fixture
async def owner_and_character(session: AsyncSession):
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    character = Character(
        project_id=project.id,
        name="Mèo",
        speaker_tag_original="speaker_1",
        voice_sample_gcs_uri="gs://bucket/characters/mèo/sample.wav",
    )
    session.add(character)
    await session.flush()

    return owner, character


async def test_get_character_voice_sample_url_returns_signed_url(
    session: AsyncSession, owner_and_character, monkeypatch
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router

    owner, character = owner_and_character
    captured_uri = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured_uri["value"] = gcs_uri
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        characters_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    response = await characters_router.get_character_voice_sample_url(
        character_id=character.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.url == "https://fake-signed-read-url.test/"
    assert captured_uri["value"] == character.voice_sample_gcs_uri


async def test_create_character_creates_row_with_manual_speaker_tag(session: AsyncSession):
    """The "add a voice by uploading audio" feature — creates a character
    manually, independent of the automatic diarization result.
    speaker_tag_original must have the "manual_" + uuid prefix, and must NOT
    collide with the real "speaker_N" format that VoiceProfilingAgent might
    encounter later (existing_by_tag matches by string — see
    voice_profiling_agent.py), avoiding this manually created character being
    accidentally "mistaken" for an automatically detected speaker."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import CharacterCreate

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    result = await characters_router.create_character(
        payload=CharacterCreate(project_id=project.id, name="Giọng mới"),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.name == "Giọng mới"
    assert result.voice_sample_gcs_uri is None
    character_row = await session.get(Character, result.id)
    assert character_row.speaker_tag_original.startswith("manual_")


async def test_create_character_404_for_other_users_project(session: AsyncSession):
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import CharacterCreate

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    stranger = User(
        firebase_uid=f"stranger-{uuid.uuid4()}", email=f"stranger-{uuid.uuid4()}@test.local"
    )
    session.add_all([owner, stranger])
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await characters_router.create_character(
            payload=CharacterCreate(project_id=project.id, name="Giọng mới"),
            session=session,
            user=CurrentUser(
                id=stranger.id, firebase_uid=stranger.firebase_uid, email=stranger.email
            ),
        )
    assert exc_info.value.status_code == 404


async def test_create_voice_sample_upload_url_sets_gcs_uri_before_upload(
    session: AsyncSession, monkeypatch
):
    """Same pattern as create_upload_url in projects.py: set
    voice_sample_gcs_uri IMMEDIATELY when the signed URL is generated (before
    the bytes actually reach GCS) — the frontend always finishes the PUT
    before calling the next classify step."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import UploadUrlRequest

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    character = Character(
        project_id=project.id, name="X", speaker_tag_original=f"manual_{uuid.uuid4()}"
    )
    session.add(character)
    await session.flush()

    async def fake_generate_signed_url(blob_name: str, **kwargs):
        return f"https://fake-upload-url.test/{blob_name}"

    monkeypatch.setattr(characters_router._storage, "generate_signed_url", fake_generate_signed_url)

    response = await characters_router.create_voice_sample_upload_url(
        character_id=character.id,
        payload=UploadUrlRequest(filename="sample.wav", content_type="audio/wav"),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.upload_url.startswith("https://fake-upload-url.test/")
    assert response.gcs_uri == character.voice_sample_gcs_uri
    assert character.voice_sample_gcs_uri is not None


async def test_classify_voice_sample_schedules_local_task_when_sample_uploaded(
    session: AsyncSession, monkeypatch
):
    from fastapi import BackgroundTasks

    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router

    monkeypatch.setattr(settings, "use_local_pubsub", True)

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    character = Character(
        project_id=project.id,
        name="X",
        speaker_tag_original=f"manual_{uuid.uuid4()}",
        voice_sample_gcs_uri="gs://bucket/manual-sample.wav",
    )
    session.add(character)
    await session.flush()

    background_tasks = BackgroundTasks()
    result = await characters_router.classify_voice_sample(
        character_id=character.id,
        background_tasks=background_tasks,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.id == character.id
    assert len(background_tasks.tasks) == 1


async def test_classify_voice_sample_400_when_no_sample_uploaded_yet(session: AsyncSession):
    from fastapi import BackgroundTasks

    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    character = Character(
        project_id=project.id, name="X", speaker_tag_original=f"manual_{uuid.uuid4()}"
    )
    session.add(character)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await characters_router.classify_voice_sample(
            character_id=character.id,
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 400


async def test_get_character_voice_sample_url_404_when_no_sample_yet(
    session: AsyncSession, owner_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router

    owner, character = owner_and_character
    character.voice_sample_gcs_uri = None
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await characters_router.get_character_voice_sample_url(
            character_id=character.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_list_voice_options_returns_the_12_prebuilt_voices(session: AsyncSession):
    """specs/003-script-first-ingestion US2 — a human must be able to pick a
    character's voice directly from a fixed list, with no audio sample and no
    Gemini call involved in listing the options themselves."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    result = await characters_router.list_voice_options(
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email)
    )

    assert len(result) == 12
    names = {option.voice_name for option in result}
    assert "Puck" in names  # one of the known prebuilt voices (gemini_client.py)
    assert all(option.description for option in result)


async def test_assign_character_voice_sets_metadata_with_no_gemini_call(
    session: AsyncSession, owner_and_character
):
    """specs/003-script-first-ingestion US2 — assigning a voice manually must
    not require an audio sample or a Gemini classification call (contrast
    with classify_voice_sample, which always requires both)."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import VoiceAssignment

    owner, character = owner_and_character

    result = await characters_router.assign_character_voice(
        character_id=character.id,
        payload=VoiceAssignment(voice_name="Puck"),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.voice_name == "Puck"
    character_row = await session.get(Character, character.id)
    assert character_row.voice_profile_metadata == {
        "voice_name": "Puck",
        "manually_assigned": True,
    }


async def test_assign_character_voice_400_for_unknown_voice_name(
    session: AsyncSession, owner_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import VoiceAssignment

    owner, character = owner_and_character

    with pytest.raises(HTTPException) as exc_info:
        await characters_router.assign_character_voice(
            character_id=character.id,
            payload=VoiceAssignment(voice_name="NotARealVoice"),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 400


async def test_assign_character_voice_404_for_other_users_character(
    session: AsyncSession, owner_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import characters as characters_router
    from api.schemas.schemas import VoiceAssignment

    _owner, character = owner_and_character
    stranger = User(
        firebase_uid=f"stranger-{uuid.uuid4()}", email=f"stranger-{uuid.uuid4()}@test.local"
    )
    session.add(stranger)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await characters_router.assign_character_voice(
            character_id=character.id,
            payload=VoiceAssignment(voice_name="Puck"),
            session=session,
            user=CurrentUser(
                id=stranger.id, firebase_uid=stranger.firebase_uid, email=stranger.email
            ),
        )
    assert exc_info.value.status_code == 404
