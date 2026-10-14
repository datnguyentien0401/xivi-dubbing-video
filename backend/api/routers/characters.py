"""Endpoints for managing character/voice profiles (Character Voice Manager)."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.firebase_auth import CurrentUser, get_current_user
from api.core.local_pubsub_client import schedule_local_voice_sample_uploaded
from api.core.ownership import get_owned_character, get_owned_project
from api.core.pubsub_client import publish_voice_sample_uploaded
from api.db.models import Character
from api.db.session import get_session
from api.schemas.schemas import (
    CharacterCreate,
    CharacterOut,
    CharacterUpdate,
    SignedReadUrlResponse,
    UploadUrlRequest,
    VoiceAssignment,
    VoiceOption,
    VoiceSampleUploadUrlResponse,
)
from worker.services.gemini_client import list_prebuilt_voices
from worker.services.storage import build_storage_client

router = APIRouter(prefix="/characters", tags=["characters"])

# Shared instance, avoids recreating storage.Client() on every request — see
# api/routers/projects.py (same pattern).
_storage = build_storage_client()


@router.get("", response_model=list[CharacterOut])
async def list_characters(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    await get_owned_project(
        session, project_id, user.id
    )  # 404 if the project isn't owned by the user
    result = await session.execute(select(Character).where(Character.project_id == project_id))
    return result.scalars().all()


@router.get("/voice-options", response_model=list[VoiceOption])
async def list_voice_options(user: CurrentUser = Depends(get_current_user)):
    """The 12 prebuilt TTS voices, for a human to pick from directly when
    assigning a character's voice manually (specs/003-script-first-ingestion)
    — no project/character scoping needed, this is a fixed, global list, not
    per-project data. Still requires auth (consistent with every other
    endpoint here), just doesn't need an ownership check since nothing
    project-specific is being read."""
    return list_prebuilt_voices()


@router.put("/{character_id}/voice", response_model=CharacterOut)
async def assign_character_voice(
    character_id: uuid.UUID,
    payload: VoiceAssignment,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Manually assign a character's voice directly from the prebuilt list —
    no audio sample, no Gemini classification call (contrast with
    classify_voice_sample above, which always requires an uploaded sample).
    Used by script-first job creation (specs/003-script-first-ingestion,
    US2) where there's no original recording for Gemini to listen to and
    match automatically."""
    character = await get_owned_character(session, character_id, user.id)
    valid_voice_names = {option.voice_name for option in list_prebuilt_voices()}
    if payload.voice_name not in valid_voice_names:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{payload.voice_name}' is not a valid prebuilt voice name",
        )
    character.voice_profile_metadata = {
        "voice_name": payload.voice_name,
        "manually_assigned": True,
    }
    await session.commit()
    await session.refresh(character)
    return character


@router.post("", response_model=CharacterOut, status_code=status.HTTP_201_CREATED)
async def create_character(
    payload: CharacterCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a character manually — used when adding a new voice by uploading
    a sample audio directly (see create_voice_sample_upload_url + classify_voice_sample
    below), independent of Agent 1/2's automatic diarization result."""
    await get_owned_project(
        session, payload.project_id, user.id
    )  # 404 if the project isn't owned by the user
    character = Character(
        project_id=payload.project_id,
        name=payload.name,
        # speaker_tag_original doesn't apply to manually created characters
        # (not tied to any speaker_tag from transcription) — use the
        # "manual_" prefix + uuid so it will NEVER collide with a real
        # "speaker_N"-style tag that VoiceProfilingAgent might encounter later
        # (existing_by_tag matches by string — see voice_profiling_agent.py).
        speaker_tag_original=f"manual_{uuid.uuid4()}",
    )
    session.add(character)
    await session.commit()
    await session.refresh(character)
    return character


@router.post("/{character_id}/voice-sample-upload-url", response_model=VoiceSampleUploadUrlResponse)
async def create_voice_sample_upload_url(
    character_id: uuid.UUID,
    payload: UploadUrlRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Generate a signed URL so the frontend can upload the voice sample
    directly to GCS (bypassing the backend) — same pattern as create_upload_url
    in api/routers/projects.py. Set voice_sample_gcs_uri IMMEDIATELY (before
    the bytes actually reach GCS) because the frontend always finishes the PUT
    before calling the next step (classify_voice_sample)."""
    character = await get_owned_character(session, character_id, user.id)
    blob_name = f"characters/{character_id}/manual-sample/{uuid.uuid4()}-{payload.filename}"
    upload_url = await _storage.generate_signed_url(blob_name, content_type=payload.content_type)
    character.voice_sample_gcs_uri = _storage.blob_uri(blob_name)
    await session.commit()
    return VoiceSampleUploadUrlResponse(
        upload_url=upload_url, gcs_uri=character.voice_sample_gcs_uri
    )


@router.post(
    "/{character_id}/classify-voice-sample",
    response_model=CharacterOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def classify_voice_sample(
    character_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Trigger the worker to listen to the just-uploaded sample and pick the
    closest TTS voice (asynchronous, see AGENTS.md #4) — call this AFTER the
    frontend has finished PUTting the file to the upload_url from the step
    above. voice_name in the response may still hold its old value/None until
    the worker finishes processing (the frontend refetches afterward)."""
    character = await get_owned_character(session, character_id, user.id)
    if not character.voice_sample_gcs_uri:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No voice sample has been uploaded for this character yet"
        )
    if settings.use_local_pubsub:
        schedule_local_voice_sample_uploaded(background_tasks, str(character_id))
    else:
        publish_voice_sample_uploaded(str(character_id))
    return character


@router.get("/{character_id}/voice-sample-url", response_model=SignedReadUrlResponse)
async def get_character_voice_sample_url(
    character_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Signed read URL so AudioPlayer.tsx can play the voice sample — same
    reason as get_dialogue_line_audio_url in api/routers/jobs.py (the GCS
    bucket is not public-read)."""
    character = await get_owned_character(session, character_id, user.id)
    if not character.voice_sample_gcs_uri:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This character has no voice sample yet")
    url = await _storage.generate_signed_read_url(character.voice_sample_gcs_uri)
    return SignedReadUrlResponse(url=url)


@router.patch("/{character_id}", response_model=CharacterOut)
async def update_character(
    character_id: uuid.UUID,
    payload: CharacterUpdate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    character = await get_owned_character(session, character_id, user.id)
    character.name = payload.name
    await session.commit()
    await session.refresh(character)
    return character
