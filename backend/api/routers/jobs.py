"""Endpoints for managing pipeline processing jobs.

Per principle #4 in AGENTS.md: every long-running task is asynchronous.
POST /jobs only creates the record + publishes to Pub/Sub, it does NOT run
the pipeline immediately.
"""

import asyncio
import logging
import os
import re
import tempfile
import uuid
import zipfile

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.firebase_auth import CurrentUser, get_current_user
from api.core.local_pubsub_client import (
    schedule_local_job_attach_video_requested,
    schedule_local_job_created,
    schedule_local_job_export_requested,
)
from api.core.ownership import (
    get_owned_character,
    get_owned_dialogue_line,
    get_owned_job,
    get_owned_source_video,
)
from api.core.pubsub_client import (
    publish_job_attach_video_requested,
    publish_job_created,
    publish_job_export_requested,
)
from api.db.models import (
    Character,
    DialogueLine,
    Job,
    Project,
    QuizItem,
    SourceVideo,
    VocabularyItem,
)
from api.db.session import get_session
from api.schemas.schemas import (
    AttachVideoRequest,
    CharacterOut,
    Clip,
    DialogueLineOut,
    DialogueLineUpdate,
    DialogueLinesBatchUpdate,
    EducationalContentOut,
    JobCreate,
    JobOut,
    SignedReadUrlResponse,
    TimelineState,
    TimelineUpdate,
    Track,
)
from worker.services.mux_service import MIN_DIALOGUE_SLOT_SECONDS
from worker.services.storage import build_storage_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])
# Separate route, WITHOUT the /jobs prefix — the spec requires exactly the
# path `PATCH /dialogue-lines/{id}` (not /jobs/dialogue-lines/{id}), see
# specs/001-dubbing-pipeline/tasks.md.
dialogue_lines_router = APIRouter(prefix="/dialogue-lines", tags=["jobs"])

# Shared instance, avoids recreating storage.Client() on every request — see
# api/routers/projects.py (same pattern).
_storage = build_storage_client()

# 7 days is the maximum expiration GCS V4 signed URLs allow (raises
# ValueError if exceeded) — minus 1 hour to avoid cutting it close due to
# clock drift between URL generation time and when GCS actually checks it.
# Much longer than get_job_video_url (60') because the link from
# get_job_share_url is meant to be SHARED EXTERNALLY (downloaded, posted on
# social media) — not just viewed while the review page happens to be open.
_SHARE_URL_EXPIRATION_MINUTES = 7 * 24 * 60 - 60


@router.post("", response_model=list[JobOut], status_code=status.HTTP_202_ACCEPTED)
async def create_jobs(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Create one job per target language, publish a message for the worker to process."""
    source_video = await get_owned_source_video(session, payload.source_video_id, user.id)
    jobs = [
        Job(
            project_id=source_video.project_id,
            source_video_id=payload.source_video_id,
            target_language=lang,
        )
        for lang in payload.target_languages
    ]
    session.add_all(jobs)
    await session.commit()
    for job in jobs:
        await session.refresh(job)
        if settings.use_local_pubsub:
            # Local dev — no real Pub/Sub, call the worker directly over HTTP
            # via BackgroundTasks (doesn't block the response, see
            # api/core/local_pubsub_client.py).
            schedule_local_job_created(background_tasks, str(job.id))
        else:
            publish_job_created(str(job.id))
    return jobs


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    return await get_owned_job(session, job_id, user.id)


def _effective_video_gcs_uri(job: Job) -> str | None:
    """The job's "effective" result video — prefers the Video Editor export
    (`exported_video_gcs_uri`, specs/002-video-editor) if the creator has
    exported one, falls back to the original AI output (`muxed_video_gcs_uri`)
    if no export exists yet. Shared by every place that serves the "result
    video" externally (view/download/share/publish) instead of repeating this
    priority logic in each individual endpoint — see US5/US6 in
    specs/002-video-editor/spec.md: the original AI output is never
    overwritten, it's only used as a fallback when no export exists yet."""
    return job.exported_video_gcs_uri or job.muxed_video_gcs_uri


@router.get("/{job_id}/video-url", response_model=SignedReadUrlResponse)
async def get_job_video_url(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Signed read URL for the result video (prefers the export, falls back to
    the original AI output — see `_effective_video_gcs_uri`) — previously
    there was no way to view the result video anywhere: ReviewDashboard.tsx
    only let you listen to audio per line, it didn't show the final video
    (bug fixed, same reason as get_dialogue_line_audio_url: the GCS bucket is
    not public-read)."""
    job = await get_owned_job(session, job_id, user.id)
    video_uri = _effective_video_gcs_uri(job)
    if not video_uri:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This job has no result video yet")
    url = await _storage.generate_signed_read_url(video_uri)
    return SignedReadUrlResponse(url=url)


@router.get("/{job_id}/share-url", response_model=SignedReadUrlResponse)
async def get_job_share_url(
    job_id: uuid.UUID,
    download: bool = False,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Shareable link for the result video — used by the Copy Link/Share/Download
    buttons in ReviewDashboard.tsx. Differs from get_job_video_url (60' expiry,
    only meant for viewing while the page happens to be open) in 2 ways: a
    much longer expiry (nearly 7 days — long enough to paste into a message/post
    on social media and have the recipient open it later without it expiring
    right away), and it's ONLY granted once the job is published — "handing the
    link out externally" is itself a form of publishing, which must follow the
    human review step (see AGENTS.md principle #5, same reason approve_job
    below exists)."""
    job = await get_owned_job(session, job_id, user.id)
    if job.status != "published":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This job hasn't been approved/published yet, cannot create a share link",
        )
    video_uri = _effective_video_gcs_uri(job)
    if not video_uri:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This job has no result video yet")

    download_filename = None
    if download:
        project_name = (
            await session.execute(select(Project.name).where(Project.id == job.project_id))
        ).scalar_one()
        # Keep only letters/digits, replace everything else with "_" — a
        # download filename shouldn't contain characters that trip up the
        # browser/OS (spaces, "/", emoji a user happened to type into the
        # project name).
        safe_name = re.sub(r"[^\w\-]+", "_", project_name).strip("_") or "xivi"
        download_filename = f"{safe_name}-{job.target_language}.mp4"

    url = await _storage.generate_signed_read_url(
        video_uri,
        expiration_minutes=_SHARE_URL_EXPIRATION_MINUTES,
        download_filename=download_filename,
    )
    return SignedReadUrlResponse(url=url, expires_in_seconds=_SHARE_URL_EXPIRATION_MINUTES * 60)


@router.get("/{job_id}/dialogue-lines", response_model=list[DialogueLineOut])
async def list_dialogue_lines(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    await get_owned_job(session, job_id, user.id)  # 404 if the job isn't owned by the user
    result = await session.execute(
        select(DialogueLine).where(DialogueLine.job_id == job_id).order_by(DialogueLine.start_time)
    )
    return result.scalars().all()


@router.patch("/{job_id}/dialogue-lines/batch", response_model=list[DialogueLineOut])
async def batch_update_dialogue_lines(
    job_id: uuid.UUID,
    payload: DialogueLinesBatchUpdate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Batch edit multiple dialogue lines at once in a single transaction.

    Used by ReviewDashboard's 'Save all translated script' feature. If any
    updated line ends up needing redub (safety_status == 'ok', has translated_text,
    no dubbed_audio_gcs_uri), moves job.current_stage to 'safety_checked' and
    wakes up the worker via Pub/Sub ONCE for the entire batch. DubbingAgent
    will re-dub only the lines whose audio was invalidated, and MuxService
    will re-mux the video with the new audio clips.
    """
    job = await get_owned_job(session, job_id, user.id)
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job hasn't reached the review stage yet (still processing), "
            "cannot edit dialogue lines",
        )

    line_ids = [item.id for item in payload.lines]
    stmt = (
        select(DialogueLine)
        .where(
            DialogueLine.job_id == job_id,
            DialogueLine.id.in_(line_ids),
        )
        .order_by(DialogueLine.start_time)
    )
    result = await session.execute(stmt)
    lines_by_id = {line.id: line for line in result.scalars().all()}

    if len(lines_by_id) != len(set(line_ids)):
        missing = set(line_ids) - set(lines_by_id.keys())
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Dialogue lines not found for this job: {missing}",
        )

    any_needs_redub = False
    updated_lines: list[DialogueLine] = []
    project_characters: set[uuid.UUID] | None = None

    for item in payload.lines:
        line = lines_by_id[item.id]

        if (
            item.translated_text is None
            and item.character_id is None
            and item.safety_status is None
        ):
            continue

        changed = False

        if item.translated_text is not None and item.translated_text != line.translated_text:
            line.translated_text = item.translated_text
            line.dubbed_audio_gcs_uri = None  # invalidated — old audio read old text
            changed = True

        if item.character_id is not None and item.character_id != line.character_id:
            if project_characters is None:
                chars_res = await session.execute(
                    select(Character.id).where(Character.project_id == job.project_id)
                )
                project_characters = set(chars_res.scalars().all())

            if item.character_id not in project_characters:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"Character {item.character_id} does not belong to this project",
                )
            line.character_id = item.character_id
            line.dubbed_audio_gcs_uri = None  # invalidated — old audio used old voice
            changed = True

        if item.safety_status is not None and item.safety_status != line.safety_status:
            line.safety_status = item.safety_status
            line.safety_reasoning = (
                "Manually marked safe by the reviewer."
                if item.safety_status == "ok"
                else "Manually flagged by the reviewer."
            )
            if item.safety_status == "flagged":
                line.dubbed_audio_gcs_uri = None
            changed = True

        if (
            line.safety_status == "ok"
            and bool(line.translated_text)
            and not line.dubbed_audio_gcs_uri
        ):
            any_needs_redub = True

        if changed:
            updated_lines.append(line)

    await session.commit()
    for l in updated_lines:
        await session.refresh(l)

    if any_needs_redub:
        job.current_stage = "safety_checked"
        job.exported_video_gcs_uri = None
        await session.commit()
        if settings.use_local_pubsub:
            schedule_local_job_created(background_tasks, str(job.id))
        else:
            publish_job_created(str(job.id))

    return updated_lines


@router.get("/{job_id}/educational-content", response_model=EducationalContentOut)
async def get_educational_content(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Vocabulary + quiz generated by Agent 7 (EducationalAgent) — see
    docs/frontend-spec.md 3.6 (EduPreview.tsx). Agent 7 runs off the critical
    path and never fails the pipeline, so both lists may legitimately still be
    empty even once the job reaches ready_for_review."""
    await get_owned_job(session, job_id, user.id)  # 404 if the job isn't owned by the user
    vocabulary = (
        (await session.execute(select(VocabularyItem).where(VocabularyItem.job_id == job_id)))
        .scalars()
        .all()
    )
    quiz = (
        (await session.execute(select(QuizItem).where(QuizItem.job_id == job_id))).scalars().all()
    )
    return EducationalContentOut(vocabulary=vocabulary, quiz=quiz)


@router.get("/{job_id}/characters", response_model=list[CharacterOut])
async def list_job_characters(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """List of characters belonging to this job's project — used for the
    character/voice dropdown in ReviewDashboard.tsx (changing character_id for
    a given dialogue line, see update_dialogue_line below)."""
    job = await get_owned_job(session, job_id, user.id)  # 404 if the job isn't owned by the user
    # Reads Job.project_id directly (not by joining through source_video_id)
    # since specs/003-script-first-ingestion — a script-first job has no
    # source_video_id, so the old join-through-video approach would always
    # return an empty list for it, even though characters were already
    # assigned at script-job creation time.
    result = await session.execute(select(Character).where(Character.project_id == job.project_id))
    return result.scalars().all()


async def _build_default_timeline(session: AsyncSession, job: Job) -> TimelineState:
    """Build a default timeline snapshot from dialogue_lines when
    `job.timeline_edits` is still the `{}` sentinel (the editor has never
    been opened for this job) — see Clarifications #1 in
    specs/002-video-editor/spec.md.

    Track -> source asset mapping convention (no dedicated field on Clip for
    this since every job has exactly one source video, see the "Layering
    multiple video tracks" Out of Scope item): the "video" and
    "audio_original" tracks both implicitly reference the job's
    `source_video.gcs_uri` (using the original audio as-is, no background
    music separation — see Out of Scope); each clip in the "dialogue" track
    references `dialogue_line.dubbed_audio_gcs_uri` via `dialogue_line_id`.
    `source_start`/`source_end` are always computed against that clip's OWN
    source asset (standard timeline-editor convention — in/out points
    relative to the clip's own source, not a shared timeline reference point),
    not against the job's overall timeline.
    """
    source_video = await session.get(SourceVideo, job.source_video_id)
    lines = (
        (
            await session.execute(
                select(DialogueLine)
                .where(DialogueLine.job_id == job.id)
                .order_by(DialogueLine.start_time)
            )
        )
        .scalars()
        .all()
    )

    duration = source_video.duration_seconds if source_video else None
    if not duration:
        # SourceVideo.duration_seconds may not be set yet (e.g. hasn't fully
        # gone through Agent 1) — fall back to the last dialogue line's end
        # time, good enough for the MVP instead of outright blocking the
        # editor from opening.
        duration = max((line.end_time for line in lines), default=0.0)
    if duration <= 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Video duration could not be determined yet, cannot open the editor",
        )

    tracks = [
        Track(
            id="video",
            type="video",
            clips=[
                Clip(id="video-source", timeline_start=0.0, source_start=0.0, source_end=duration)
            ],
        ),
        Track(
            id="audio_original",
            type="audio_original",
            clips=[
                Clip(
                    id="audio-original-source",
                    timeline_start=0.0,
                    source_start=0.0,
                    source_end=duration,
                )
            ],
        ),
    ]

    lines_by_character: dict[uuid.UUID, list[DialogueLine]] = {}
    for line in lines:
        # Dialogue lines without an assigned character (character_id NULL)
        # are skipped in this default snapshot — Track type="dialogue"
        # requires character_id (see schemas.py::Track), and in practice by
        # the time a job reaches "ready_for_review" every line should already
        # have been assigned a character by VoiceProfilingAgent. We accept
        # this limitation for the MVP instead of adding a separate
        # "unassigned" track type.
        if line.character_id is not None:
            lines_by_character.setdefault(line.character_id, []).append(line)

    for character_id, char_lines in lines_by_character.items():
        tracks.append(
            Track(
                id=f"character-{character_id}",
                type="dialogue",
                character_id=character_id,
                clips=[
                    Clip(
                        id=f"dialogue-{line.id}",
                        dialogue_line_id=line.id,
                        timeline_start=line.start_time,
                        source_start=0.0,
                        # The dubbed audio has been time-stretched (atempo) by
                        # mux_service to fit exactly (end_time - start_time)
                        # seconds — see
                        # worker/services/mux_service.py::_encode_audio_track
                        # — so the clip's default source_end is exactly that
                        # length, not the audio file's original length. Uses
                        # the SAME minimum-floor constant as
                        # _encode_audio_track (rather than picking a different
                        # number) to match the slot_duration actually used
                        # during muxing.
                        source_end=max(line.end_time - line.start_time, MIN_DIALOGUE_SLOT_SECONDS),
                    )
                    for line in char_lines
                ],
            )
        )

    try:
        return TimelineState(duration_seconds=duration, tracks=tracks)
    except ValidationError:
        # Can only happen due to inconsistent dialogue_lines data (e.g. 2
        # lines with the SAME character_id overlapping in time — a bug at
        # the transcription layer, not a caller input error) — raise a clear
        # error instead of exposing a raw 500/Pydantic stack trace to the
        # frontend.
        logger.exception("job %s: dialogue_lines data could not build a default timeline", job.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Dialogue line data is inconsistent, could not build a default timeline",
        ) from None


@router.get("/{job_id}/timeline", response_model=TimelineState)
async def get_job_timeline(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Return the saved `timeline_edits`, or build a default snapshot if the
    job's editor has never been opened (see `_build_default_timeline`). The
    job must be in the `ready_for_review` stage — the editor can still be
    opened after publishing (Clarifications #3 in spec.md: current_stage
    doesn't change on publish, only `status` does), but CANNOT be opened while
    the job is still processing or re-dubbing a single line (in which case
    current_stage is temporarily moved back to "safety_checked" — see
    update_dialogue_line)."""
    job = await get_owned_job(session, job_id, user.id)
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job isn't ready for the editor yet (still processing or re-dubbing a line)",
        )
    if job.timeline_edits:
        try:
            return TimelineState.model_validate(job.timeline_edits)
        except ValidationError:
            # timeline_edits isn't validated at the DB layer (raw JSONB) — if
            # it was ever written incorrectly (a future bug, a manual DB edit,
            # an incompatible schema_version change), this is the ONLY
            # safeguard that catches it, see the comment at the top of the
            # "Video Editor" block in schemas.py. Raise a clear error instead
            # of exposing a raw 500/Pydantic stack trace to the frontend.
            logger.exception("job %s: saved timeline_edits is invalid", job.id)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "The saved timeline data is invalid, please contact support",
            ) from None
    return await _build_default_timeline(session, job)


@router.patch("/{job_id}/timeline", response_model=TimelineState)
async def update_job_timeline(
    job_id: uuid.UUID,
    payload: TimelineUpdate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Debounced autosave from VideoEditor.tsx — the client always sends the
    ENTIRE new snapshot (not a diff), overwriting `timeline_edits`. Only
    UPDATEs this exact column (not via `session.add`/the ORM attribute on the
    `job` held in the session) to avoid a conflict if the worker is writing
    `current_stage` on this SAME job at nearly the same time — see Technical
    Risks in plan.md.

    Does NOT handle lost updates between 2 autosave PATCHes from THIS SAME
    editor fired nearly simultaneously (e.g. an older request delayed by the
    network, whose response arrives after a newer request's) — acceptable
    since spec.md's Out of Scope section already assumes "only one person
    edits a given job at a time" (concurrent multi-user editing of one job is
    not handled), so no optimistic concurrency/version column is needed for
    the MVP."""
    job = await get_owned_job(session, job_id, user.id)  # 404/403 if not owned by the user
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job isn't ready for the editor yet (still processing or re-dubbing a line)",
        )
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(timeline_edits=payload.timeline.model_dump(mode="json"))
    )
    await session.commit()
    return payload.timeline


@router.post("/{job_id}/export", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def export_job(
    job_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Export the final video from the current timeline (US6) — only
    publishes a message for the worker to render (`editor_export_service.py`,
    see plan.md), does NOT run ffmpeg here directly (AGENTS.md principle #4:
    every long-running task is asynchronous).

    `current_stage` temporarily switches to "exporting" during rendering —
    reuses the exact `!= "ready_for_review"` guard already present on
    GET/PATCH /jobs/{id}/timeline, so the editor is automatically locked
    (can't read/write timeline_edits) while the export is running, preventing
    autosave from overwriting a snapshot the worker is in the middle of
    reading. `main.py::_export_job` will set current_stage back to
    "ready_for_review" when done (must happen on both success and failure, or
    the job gets stuck in "exporting" forever — see the error handling there).

    Does NOT lock the row (`SELECT ... FOR UPDATE`) — 2 near-simultaneous
    POST /export requests for the SAME job (e.g. a double-click) could both
    read current_stage before either commits, resulting in 2 export messages
    being published. Acceptable for the MVP (same risk level as the rest of
    the codebase — no other place uses row locking either) and low impact
    (the later message's worker processing overwrites the earlier one's
    result, no other dangerous side effect).

    Blocked for a script-first job with no video attached yet
    (specs/003-script-first-ingestion) — there is no video/timeline to
    export at all until POST /jobs/{id}/attach-video exists and runs; found
    via code-reviewer, review 2026-10-11: without this guard, `pipeline.py`
    now lets such a job reach `ready_for_review` (previously unreachable),
    and worker/main.py::_export_job would crash with AttributeError
    dereferencing a None SourceVideo."""
    job = await get_owned_job(session, job_id, user.id)
    if job.source_video_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This job has no video yet — exporting isn't available for an "
            "audio-only job until a video is attached",
        )
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job isn't ready to export yet (still processing, re-dubbing a "
            "line, or already exporting)",
        )
    if not job.timeline_edits:
        # The editor has never been opened — use the default snapshot as the
        # export input, reusing the exact default-building logic from
        # GET /jobs/{id}/timeline rather than having the worker rebuild it
        # itself (the worker should only be concerned with rendering, see
        # plan.md).
        default_timeline = await _build_default_timeline(session, job)
        job.timeline_edits = default_timeline.model_dump(mode="json")
    job.current_stage = "exporting"
    await session.commit()
    await session.refresh(job)

    if settings.use_local_pubsub:
        schedule_local_job_export_requested(background_tasks, str(job.id))
    else:
        publish_job_export_requested(str(job.id))

    return job


@router.post("/{job_id}/attach-video", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def attach_video(
    job_id: uuid.UUID,
    payload: AttachVideoRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Attach a real video to an already-dubbed script-first job (US5,
    specs/003-script-first-ingestion) — the final step that brings such a
    job to the same finished state a video-first job reaches on its own.
    Only publishes a message for the worker to mux (AGENTS.md principle #4);
    does NOT run ffmpeg here.

    `current_stage` temporarily switches to "muxing" — mirrors `export_job`'s
    "exporting" transient stage exactly: `worker/main.py::_attach_video` sets
    it back to "ready_for_review" when done, on both success and failure.

    Blocked only once a video has been successfully muxed
    (`muxed_video_gcs_uri is not None`) — deliberately NOT blocked merely by
    `source_video_id is not None`, so a FAILED attach (muxing failed, e.g. a
    corrupt upload or an ffmpeg error — `source_video_id` stays set from the
    failed attempt, but `muxed_video_gcs_uri` stays empty, see
    `worker/main.py::_attach_video`'s `finally` block) can be retried with a
    new upload, rather than leaving the job permanently stuck with no path
    forward (found via code-reviewer, review 2026-10-11: the frontend had no
    way to distinguish "attach succeeded" from "attach failed" using
    `current_stage` alone, and the old, stricter guard here made that failure
    permanent and unrecoverable). Also only valid once the job is actually
    fully dubbed (`current_stage == "ready_for_review"`) — attaching a video
    to a job that's still mid-pipeline would have nothing real to mux yet.

    Does NOT lock the row, same accepted trade-off as export_job above (a
    double-click could publish 2 "job_attach_video" messages) — the second
    request would still fail the `current_stage != "ready_for_review"` check
    the moment the first one commits (current_stage is "muxing" by then),
    which is the same narrow window export_job's equivalent risk accepts."""
    job = await get_owned_job(session, job_id, user.id)
    if job.source_video_id is not None and job.muxed_video_gcs_uri is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This job already has a video attached")
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job must be fully dubbed (ready for review) before attaching a video",
        )
    source_video = await get_owned_source_video(session, payload.source_video_id, user.id)
    if source_video.project_id != job.project_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This video does not belong to this job's project"
        )

    job.source_video_id = payload.source_video_id
    job.current_stage = "muxing"
    await session.commit()
    await session.refresh(job)

    if settings.use_local_pubsub:
        schedule_local_job_attach_video_requested(background_tasks, str(job.id))
    else:
        publish_job_attach_video_requested(str(job.id))

    return job


def _write_zip(zip_path: str, local_paths_and_names: list[tuple[str, str]]) -> None:
    """Blocking helper run via asyncio.to_thread by get_dubbed_audio_bundle
    below — kept as a plain sync function so the CPU-bound DEFLATE work
    never runs directly on the event loop."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for local_path, archive_name in local_paths_and_names:
            zip_file.write(local_path, arcname=archive_name)


@router.get("/{job_id}/dubbed-audio-bundle", response_model=SignedReadUrlResponse)
async def get_dubbed_audio_bundle(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """FR-008 (specs/003-script-first-ingestion) — a named, downloadable set
    of every dubbed dialogue line's audio, traceable back to the script's
    segment identifiers. Mirrors the manual dubbing process's own handoff
    step ("name the audio files into folders, hand off to the editor", see
    docs/manual-dubbing-process.docx) — this is the deliverable for a
    script-first job before a video is ever attached, but works identically
    for a video-first job too (just named by index instead of segment_code,
    since transcription never produces one — see ingestion_agent.py).

    Downloads via the same StorageClient already used throughout this file
    for signed-URL generation, zips in a temp dir, uploads the zip, and
    returns a signed URL — the same lightweight, synchronous pattern as
    get_dialogue_line_audio_url below, not a new async Pub/Sub job: the
    zipped content is already-finished small audio clips (no Gemini/ffmpeg
    work happens here), bounded by this job's own dialogue line count."""
    await get_owned_job(session, job_id, user.id)  # 404 if the job isn't owned by the user
    result = await session.execute(
        select(DialogueLine)
        .where(DialogueLine.job_id == job_id, DialogueLine.dubbed_audio_gcs_uri.isnot(None))
        .order_by(DialogueLine.start_time)
    )
    lines = result.scalars().all()
    if not lines:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This job has no dubbed audio yet")

    with tempfile.TemporaryDirectory(prefix=f"audio-bundle-{job_id}-") as tmp_dir:
        used_names: set[str] = set()
        local_paths_and_names: list[tuple[str, str]] = []
        for index, line in enumerate(lines):
            local_audio_path = os.path.join(tmp_dir, f"{line.id}.wav")
            await _storage.download_to_path(line.dubbed_audio_gcs_uri, local_audio_path)
            # segment_code is the natural human-facing filename (traceable
            # back to the script) — a video-first job's lines never have
            # one (only create_script_jobs sets it), so fall back to a
            # 1-based position. Defensive de-duplication: segment_code is
            # only guaranteed unique WITHIN one script import (enforced by
            # ScriptJobCreate's validator, see schemas.py) — a collision
            # should never happen today, but silently overwriting one
            # file inside the zip would be a confusing, hard-to-debug way
            # to find out that invariant broke.
            base_name = line.segment_code or f"line-{index + 1:03d}"
            archive_name = f"{base_name}.wav"
            suffix = 2
            while archive_name in used_names:
                archive_name = f"{base_name}-{suffix}.wav"
                suffix += 1
            used_names.add(archive_name)
            local_paths_and_names.append((local_audio_path, archive_name))

        zip_path = os.path.join(tmp_dir, "bundle.zip")
        # zipfile.write() is blocking disk I/O + CPU-bound DEFLATE compression —
        # offloaded to a thread so it doesn't stall the event loop (and every
        # other concurrent request on this instance) while it runs, matching
        # the asyncio.to_thread pattern gcs_client.py/local_storage_client.py
        # already use for every other blocking call in this file.
        await asyncio.to_thread(_write_zip, zip_path, local_paths_and_names)

        blob_name = f"jobs/{job_id}/dubbed-audio-bundle/{uuid.uuid4().hex[:8]}.zip"
        zip_uri = await _storage.upload_from_path(zip_path, blob_name)

    url = await _storage.generate_signed_read_url(
        zip_uri, download_filename=f"job-{job_id}-dubbed-audio.zip"
    )
    return SignedReadUrlResponse(url=url)


@dialogue_lines_router.get("/{line_id}/audio-url", response_model=SignedReadUrlResponse)
async def get_dialogue_line_audio_url(
    line_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Signed read URL so AudioPlayer.tsx can play the dubbed audio — the GCS
    bucket is not public-read so the frontend cannot construct a
    https://storage.googleapis.com/... URL on its own (we manually reproduced
    a 403 while testing this)."""
    line = await get_owned_dialogue_line(session, line_id, user.id)
    if not line.dubbed_audio_gcs_uri:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This dialogue line has no audio yet")
    url = await _storage.generate_signed_read_url(line.dubbed_audio_gcs_uri)
    return SignedReadUrlResponse(url=url)


@dialogue_lines_router.patch("/{line_id}", response_model=DialogueLineOut)
async def update_dialogue_line(
    line_id: uuid.UUID,
    payload: DialogueLineUpdate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Edit the translated text, the character (voice), and/or the safety
    status for a dialogue line. Editing translated_text/character_id always
    invalidates the existing dubbed audio (clears dubbed_audio_gcs_uri);
    re-flagging a line ("ok" -> "flagged") also revokes its existing audio —
    a line a reviewer just marked unsafe must not keep playable/exportable
    audio around from before.

    The worker is only woken up when the line ends up actually READY to be
    dubbed but isn't yet (safety_status == "ok" AND has translated_text AND
    no dubbed_audio_gcs_uri) — e.g. editing ONLY the text of a still-
    "flagged" line does NOT trigger the worker on its own (DubbingAgent would
    just skip it again anyway, see dubbing_agent.py); a separate follow-up
    PATCH marking safety_status="ok" is what actually unblocks it (see the
    safety_status field docstring on DialogueLineUpdate for the real UX gap
    this fixes — a flagged line could never be re-dubbed after fixing its
    text, with no way to clear the flag at all).

    How the trigger works (when it fires): move job.current_stage back to
    "safety_checked" (the stage immediately BEFORE "dubbed" — see
    worker/pipeline.py::_STAGE_ORDER), then re-publish job_created. When
    DubbingAgent runs again it will SKIP every line that already has
    dubbed_audio_gcs_uri (see dubbing_agent.py), only dubbing the line whose
    audio was just cleared here — reusing the existing pipeline/mux as-is
    instead of writing a separate "dub a single line" flow, and the result
    video gets re-muxed as a natural consequence (the old video would
    otherwise drift out of sync if it weren't re-muxed)."""
    if (
        payload.translated_text is None
        and payload.character_id is None
        and payload.safety_status is None
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "At least one of translated_text, character_id, or safety_status is required",
        )

    line = await get_owned_dialogue_line(session, line_id, user.id)
    job = await session.get(Job, line.job_id)
    if job.current_stage != "ready_for_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job hasn't reached the review stage yet (still processing), "
            "cannot edit this dialogue line",
        )

    if payload.translated_text is not None:
        line.translated_text = payload.translated_text
        line.dubbed_audio_gcs_uri = None  # invalidated — the old audio read the old text

    if payload.character_id is not None:
        character = await get_owned_character(session, payload.character_id, user.id)
        # get_owned_character only confirms the character belongs to a
        # project owned by THIS user, it does NOT confirm that character
        # belongs to the CORRECT project of this dialogue line (a user can
        # own multiple projects) — without this check, a character from
        # project A could be mistakenly assigned to a dialogue line in
        # project B (DubbingAgent would not find that character in the job's
        # context.characters, silently falling back to the default voice
        # instead of raising a clear error).
        # Reads Job.project_id directly (not by joining through
        # source_video_id) since specs/003-script-first-ingestion — a
        # script-first job has no source_video_id, so the old
        # join-through-video approach would raise NoResultFound here instead
        # of resolving the project.
        line_project_id = (
            await session.execute(select(Job.project_id).where(Job.id == line.job_id))
        ).scalar_one()
        if character.project_id != line_project_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "This character does not belong to this dialogue line's project",
            )
        line.character_id = payload.character_id
        line.dubbed_audio_gcs_uri = None  # invalidated — the old audio used the old voice

    if payload.safety_status is not None:
        line.safety_status = payload.safety_status
        # Overwrites the AI's original reasoning (if any) — leaving the OLD
        # AI reasoning displayed after a human has explicitly overridden the
        # status would be misleading (SafetyBadge always shows `reasoning` as
        # a tooltip, see frontend-spec.md section 5 "surfacing Gen AI
        # reasoning" — this keeps that tooltip honest about who made the
        # CURRENT call).
        line.safety_reasoning = (
            "Manually marked safe by the reviewer."
            if payload.safety_status == "ok"
            else "Manually flagged by the reviewer."
        )
        if payload.safety_status == "flagged":
            line.dubbed_audio_gcs_uri = None

    # Only actually wake the worker up if the line is now in a state where
    # DubbingAgent would do something useful with it — editing just the text
    # of a still-"flagged" line clears dubbed_audio_gcs_uri above but must
    # NOT publish here, since DubbingAgent would immediately skip it again
    # anyway — that would burn a full pipeline resume (and the worker's
    # dedup lock) for nothing.
    needs_redub = (
        line.safety_status == "ok" and bool(line.translated_text) and not line.dubbed_audio_gcs_uri
    )
    await session.commit()
    await session.refresh(line)

    if needs_redub:
        job.current_stage = "safety_checked"
        job.exported_video_gcs_uri = None
        await session.commit()
        if settings.use_local_pubsub:
            schedule_local_job_created(background_tasks, str(job.id))
        else:
            publish_job_created(str(job.id))

    return line


@router.post("/{job_id}/approve", response_model=JobOut)
async def approve_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Approve + publish — previously this performed no checks at all, so it
    was possible to publish a job that had no result video whatsoever
    (muxed_video_gcs_uri still None), leaving the job published but with
    get_job_video_url/get_job_share_url always returning 404 afterward. Now
    blocked right here using `_effective_video_gcs_uri` (prefers the export,
    falls back to the original AI output — see specs/002-video-editor).

    Also blocks whenever `current_stage != "ready_for_review"` — covers 2
    cases with one check: (1) exporting (POST /jobs/{id}/export) — if the
    creator clicks Approve & Publish right while an export is running,
    publishing at that moment would immediately use the OLD
    `muxed_video_gcs_uri` (the version without the applied edits) instead of
    waiting for the newly requested export — discovered while reviewing the
    "prefer exported_video_gcs_uri" task, see the note in tasks.md; (2) an
    edit-triggered redub+re-mux (specs/003-script-first-ingestion's
    update_dialogue_line rewinds current_stage to "safety_checked" then
    republishes) — found via code-reviewer: this branch previously only
    checked for "exporting", so approving during THIS other transient
    window would silently publish the stale pre-edit video too (same root
    cause, different trigger) — the reviewer would believe they approved
    the edited version, but the published artifact wouldn't reflect it.

    A script-first job (`source_video_id is None`, specs/003-script-first-
    ingestion) has no muxed video to check for at all — the "muxed" pipeline
    stage is conditional and only runs once a real video is later attached
    (see POST /jobs/{id}/attach-video). The equivalent "is there a
    publishable result" check for that case is: every dialogue line the
    pipeline was actually expected to dub (safety_status == "ok") has
    dubbed_audio_gcs_uri set, AND at least one line actually got dubbed — a
    still-flagged line is intentionally excluded from the dub by the
    reviewer (see DubbingAgent, which only ever dubs safety_status == "ok"
    lines) and must not block approval just because it has no audio."""
    job = await get_owned_job(session, job_id, user.id)
    if job.source_video_id is None:
        if job.current_stage != "ready_for_review":
            raise HTTPException(status.HTTP_409_CONFLICT, "Job is not ready for review yet")
        lines = (
            (await session.execute(select(DialogueLine).where(DialogueLine.job_id == job.id)))
            .scalars()
            .all()
        )
        if not any(line.dubbed_audio_gcs_uri for line in lines):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "This job has no dubbed audio yet, cannot publish"
            )
        still_undubbed = [
            line for line in lines if line.safety_status == "ok" and not line.dubbed_audio_gcs_uri
        ]
        if still_undubbed:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Not every dialogue line has been dubbed yet"
            )
    else:
        if job.current_stage != "ready_for_review":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Job is still processing (exporting or re-dubbing an edit), "
                "wait for it to finish before publishing",
            )
        if not _effective_video_gcs_uri(job):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "This job has no result video yet, cannot publish"
            )
    job.status = "published"
    await session.commit()
    await session.refresh(job)
    return job
