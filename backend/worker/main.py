"""Worker service — Cloud Run service #2, receives a Pub/Sub push subscription.

Verifies the Pub/Sub OIDC token before processing (to prevent the endpoint
from being called by a forged external request) — see docs/backend-spec.md
section 5.
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import uuid

import ffmpeg
from fastapi import FastAPI, HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.cloud import firestore
from google.oauth2 import id_token
from sqlalchemy import select

from api.core.config import settings
from api.db.models import Character as CharacterRow
from api.db.models import CulturalSafetyRule, Job, Project, SourceVideo
from api.db.models import DialogueLine as DialogueLineRow
from api.db.models import VocabularyItem as VocabularyItemRow
from api.db.session import AsyncSessionLocal
from api.schemas.schemas import TimelineState
from worker.agents.dubbing_agent import DubbingAgent
from worker.agents.educational_agent import EducationalAgent
from worker.agents.ingestion_agent import IngestionAgent
from worker.agents.safety_agent import SafetyAgent
from worker.agents.script_benchmark_agent import ScriptBenchmarkAgent
from worker.agents.translation_agent import TranslationAgent
from worker.agents.voice_profiling_agent import VoiceProfilingAgent, build_voice_profile_metadata
from worker.models.pipeline_context import Character, DialogueLine, PipelineContext
from worker.pipeline import Pipeline
from worker.services.editor_export_service import export_for_job
from worker.services.gemini_client import GeminiClient
from worker.services.redis_client import RedisClient
from worker.services.storage import build_storage_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="XIVI Worker")

# --- Initialize shared services, avoid recreating them on every request ---
_redis = RedisClient(settings.redis_url)
_gemini = GeminiClient(settings.gemini_api_key, _redis)
# GCSClient or LocalStorageClient depending on settings.use_local_storage —
# see worker/services/storage.py.
_gcs = build_storage_client()

# Lazy — firestore.AsyncClient() calls google.auth.default() immediately on
# init, raising if there's no ADC yet. Defer until the first request (see
# gcs_client.py for the same reasoning).
_firestore_client: firestore.AsyncClient | None = None


def _get_firestore() -> firestore.AsyncClient:
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.AsyncClient(project=settings.google_cloud_project)
    return _firestore_client


async def _load_safety_rules() -> dict[str, str]:
    """Read the cultural_safety_rules table and merge rules by target_market —
    MUST NOT be hardcoded in code (AGENTS.md principle #6). Runs once per job;
    simpler than caching since job frequency is low (3-week MVP scope)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CulturalSafetyRule))
        rules_by_market: dict[str, list[str]] = {}
        for rule in result.scalars().all():
            rules_by_market.setdefault(rule.target_market, []).append(
                f"[{rule.severity}] {rule.rule_description}"
            )
        return {market: "\n".join(lines) for market, lines in rules_by_market.items()}


_pipeline = Pipeline(
    ingestion=IngestionAgent(_gemini, _gcs),
    script_benchmark=ScriptBenchmarkAgent(_gemini),
    voice_profiling=VoiceProfilingAgent(_gemini, _gcs),
    translation=TranslationAgent(_gemini),
    safety=SafetyAgent(_gemini, rules_loader=_load_safety_rules),
    dubbing=DubbingAgent(_gemini, _gcs),
    educational=EducationalAgent(_gemini),
    gcs=_gcs,
)


def _verify_pubsub_token(request: Request) -> None:
    if settings.use_local_pubsub:
        # Local dev — the request comes from the api service over internal
        # HTTP (see api/core/local_pubsub_client.py), not a real Pub/Sub push,
        # so there's no real OIDC token to verify. MUST NOT be enabled on a
        # real deployment (see the warning in api/core/config.py).
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing OIDC token")
    token = auth_header.removeprefix("Bearer ")
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid OIDC token") from exc
    if claims.get("aud") != settings.pubsub_push_audience:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Audience mismatch")


async def _load_context(job_id: str) -> PipelineContext:
    """Read Job + SourceVideo + Project + Character + DialogueLine (if
    resuming) from Postgres, and rebuild a PipelineContext so pipeline.py can
    continue from the right place (checkpoint/resume — see
    docs/database-schema.md section 3 on jobs.current_stage).
    """
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, uuid.UUID(job_id))
        if job is None:
            raise ValueError(f"Job {job_id} not found")

        source_video = await session.get(SourceVideo, job.source_video_id)
        # Reads Job.project_id directly (not via source_video.project_id)
        # since specs/003-script-first-ingestion — source_video is None for a
        # script-first job with no video attached yet (session.get() with a
        # None PK returns None rather than raising), which would otherwise
        # crash this shared context-loader with an AttributeError the moment
        # such a job reaches the worker.
        project = await session.get(Project, job.project_id)

        character_rows = (
            (
                await session.execute(
                    select(CharacterRow).where(CharacterRow.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        characters = [
            Character(
                id=str(c.id),
                name=c.name,
                speaker_tag_original=c.speaker_tag_original,
                voice_sample_gcs_uri=c.voice_sample_gcs_uri,
                voice_profile_metadata=c.voice_profile_metadata or {},
            )
            for c in character_rows
        ]

        line_rows = (
            (
                await session.execute(
                    select(DialogueLineRow)
                    .where(DialogueLineRow.job_id == job.id)
                    .order_by(DialogueLineRow.start_time)
                )
            )
            .scalars()
            .all()
        )
        dialogue_lines = [
            DialogueLine(
                id=str(line.id),
                character_id=str(line.character_id) if line.character_id else None,
                start_time=line.start_time,
                end_time=line.end_time,
                original_text=line.original_text,
                translated_text=line.translated_text,
                is_song=line.is_song,
                safety_status=line.safety_status,
                safety_reasoning=line.safety_reasoning,
                dubbed_audio_gcs_uri=line.dubbed_audio_gcs_uri,
            )
            for line in line_rows
        ]

        return PipelineContext(
            job_id=str(job.id),
            project_id=str(project.id),
            # None for a script-first job with no video attached yet (see
            # worker/models/pipeline_context.py) — source_video is already
            # None in that case (session.get() with a None PK returns None).
            source_video_gcs_uri=source_video.gcs_uri if source_video else None,
            source_language=project.source_language,
            target_language=job.target_language,
            current_stage=job.current_stage,
            dialogue_lines=dialogue_lines,
            characters=characters,
            muxed_video_gcs_uri=job.muxed_video_gcs_uri,
        )


async def _save_checkpoint(context: PipelineContext) -> None:
    """Write context.current_stage + dialogue_lines + characters to Postgres —
    uses session.merge() to upsert by PK (safe both when the row already
    exists from a previous checkpoint and when it was just created for the
    first time by Agent 1)."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, uuid.UUID(context.job_id))
        if job is None:
            raise ValueError(f"Job {context.job_id} not found")
        job.current_stage = context.current_stage
        job.muxed_video_gcs_uri = context.muxed_video_gcs_uri

        for character in context.characters:
            await session.merge(
                CharacterRow(
                    id=uuid.UUID(character.id),
                    project_id=uuid.UUID(context.project_id),
                    name=character.name,
                    speaker_tag_original=character.speaker_tag_original,
                    voice_sample_gcs_uri=character.voice_sample_gcs_uri,
                    voice_profile_metadata=character.voice_profile_metadata,
                )
            )

        for line in context.dialogue_lines:
            await session.merge(
                DialogueLineRow(
                    id=uuid.UUID(line.id),
                    job_id=uuid.UUID(context.job_id),
                    character_id=uuid.UUID(line.character_id) if line.character_id else None,
                    start_time=line.start_time,
                    end_time=line.end_time,
                    original_text=line.original_text,
                    translated_text=line.translated_text,
                    is_song=line.is_song,
                    safety_status=line.safety_status,
                    safety_reasoning=line.safety_reasoning,
                    dubbed_audio_gcs_uri=line.dubbed_audio_gcs_uri,
                )
            )

        await session.commit()


async def _save_educational_result(job_id: str, educational_result: dict) -> None:
    async with AsyncSessionLocal() as session:
        for item in educational_result.get("vocabulary", []):
            session.add(
                VocabularyItemRow(
                    job_id=uuid.UUID(job_id),
                    term=item["term"],
                    meaning=item["meaning"],
                    example_sentence=item["example_sentence"],
                )
            )
        await session.commit()


async def _update_realtime_status(context: PipelineContext) -> None:
    """Write context.current_stage to the Firestore doc jobs/{job_id} so the
    frontend PipelineStatus.tsx receives it in real time via a listener (no
    polling, see frontend/AGENTS.md). Fields are camelCase to match the
    frontend's JobStatus contract (frontend/src/hooks/useJobStatus.ts) —
    Postgres/Python use snake_case, but this Firestore doc is read directly
    by TS so it follows the JS convention.

    Firestore is ONLY a "current state" mirror for the UI to update quickly —
    Postgres (_save_checkpoint) remains the source of truth (see
    docs/database-schema.md section 1). So an error here MUST NOT crash the
    whole pipeline (there used to be a bug: a job that finished translating
    its first stage but where _update_realtime_status raised
    DefaultCredentialsError would lose all progress on subsequent stages, even
    though Postgres had saved it correctly) — just log and continue,
    best-effort."""
    if settings.use_local_firestore:
        # Local dev has no real Firestore — skip instead of trying to connect
        # and failing on every stage transition (wastes time, spams logs). See
        # the warning in api/core/config.py: the frontend won't receive
        # realtime updates, and will fall back to GET /jobs/{id} for manual
        # polling.
        return
    try:
        doc_ref = _get_firestore().collection("jobs").document(context.job_id)
        await doc_ref.set(
            {
                "currentStage": context.current_stage,
                "status": "processing" if context.current_stage != "ready_for_review" else "ready",
                "targetLanguage": context.target_language,
                "dialogueLineCount": len(context.dialogue_lines),
                "flaggedCount": sum(
                    1 for line in context.dialogue_lines if line.safety_status == "flagged"
                ),
            },
            merge=True,
        )
    except Exception:  # noqa: BLE001 - best-effort, must not crash the pipeline
        logger.exception(
            "job %s: failed to write Firestore realtime status (ignoring, Postgres "
            "remains the source of truth)",
            context.job_id,
        )


async def _update_export_status(
    job_id: str, *, stage: str, progress: int | None, target_language: str
) -> None:
    """Write export progress (Video Editor, specs/002-video-editor) to the
    Firestore doc jobs/{job_id} — adds an `exportProgress` field next to the
    existing `currentStage` (see _update_realtime_status above, same contract
    on the frontend's useJobStatus.ts). Same best-effort/merge=True reasoning:
    Firestore is only a UI mirror, Postgres (current_stage/exported_video_gcs_uri)
    remains the source of truth, and an error here must not break the export flow.

    There is NO real step-by-step percentage (render_export is a single
    sequential ffmpeg command that doesn't report intermediate progress) —
    only 2 honest checkpoints are written: 0 at the start, 100 when the export
    SUCCEEDS. 100 is never written on error (progress `None` means
    "unknown/stopped", to avoid incorrectly showing "100%" for a failed
    export)."""
    if settings.use_local_firestore:
        return
    try:
        doc_ref = _get_firestore().collection("jobs").document(job_id)
        payload: dict = {
            "currentStage": stage,
            "status": "processing" if stage == "exporting" else "ready",
            "targetLanguage": target_language,
        }
        if progress is not None:
            payload["exportProgress"] = progress
        await doc_ref.set(payload, merge=True)
    except Exception:  # noqa: BLE001 - best-effort, must not crash the export
        logger.exception(
            "job %s: failed to write Firestore export status (ignoring, Postgres "
            "remains the source of truth)",
            job_id,
        )


async def _update_attach_video_status(
    job_id: str,
    *,
    stage: str,
    target_language: str,
    duration_warning: str | None = None,
    mux_succeeded: bool | None = None,
) -> None:
    """Write attach-video progress (US5, specs/003-script-first-ingestion) to
    the Firestore doc jobs/{job_id} — same best-effort/merge=True contract as
    _update_export_status above. `duration_warning` surfaces a mismatch
    between the attached video's real duration and what the already-dubbed
    audio expects (see _attach_video) directly to the frontend as a
    transient, advisory signal — no new DB column needed for it, same
    reasoning as exportProgress. `mux_succeeded` is only meaningful on the
    final call (None while still in progress)."""
    if settings.use_local_firestore:
        return
    try:
        doc_ref = _get_firestore().collection("jobs").document(job_id)
        payload: dict = {
            "currentStage": stage,
            "status": "processing" if stage == "muxing" else "ready",
            "targetLanguage": target_language,
        }
        if duration_warning is not None:
            payload["durationWarning"] = duration_warning
        if mux_succeeded is not None:
            payload["muxSucceeded"] = mux_succeeded
        await doc_ref.set(payload, merge=True)
    except Exception:  # noqa: BLE001 - best-effort, must not crash attach-video
        logger.exception(
            "job %s: failed to write Firestore attach-video status (ignoring, "
            "Postgres remains the source of truth)",
            job_id,
        )


async def _classify_character_voice_sample(character_id: str) -> None:
    """Handle message type="voice_sample" — the user just uploaded an audio
    sample for a Character (see
    api/routers/characters.py::classify_voice_sample), listens to it and
    picks the closest matching TTS voice, sharing logic/fallback with
    VoiceProfilingAgent (voice_profiling_agent.py::build_voice_profile_metadata).

    Does not republish/raise if the character no longer exists or has no
    voice_sample_gcs_uri yet — this is a best-effort background job with no
    synchronous caller waiting on the result to report an error."""
    async with AsyncSessionLocal() as session:
        character_row = await session.get(CharacterRow, uuid.UUID(character_id))
        if character_row is None or not character_row.voice_sample_gcs_uri:
            logger.warning(
                "character %s: does not exist or has no voice_sample_gcs_uri yet, skipping",
                character_id,
            )
            return

        with tempfile.TemporaryDirectory(prefix=f"voice-sample-{character_id}-") as tmp_dir:
            local_path = os.path.join(tmp_dir, "sample.wav")
            await _gcs.download_to_path(character_row.voice_sample_gcs_uri, local_path)
            character_row.voice_profile_metadata = await build_voice_profile_metadata(
                _gemini, local_path, display_name=character_row.name
            )

        await session.commit()


async def _export_job(job_id: str) -> None:
    """Handle message type="job_export" (POST /jobs/{id}/export,
    specs/002-video-editor) — renders the REAL export from `jobs.timeline_edits`
    via `editor_export_service.export_for_job` (split/trim/ripple/volume/
    mute/solo/fade according to the validated timeline).

    ALWAYS resets `current_stage` to "ready_for_review" whether it succeeds or
    fails — otherwise the job would be stuck in "exporting" forever (locking
    out both the editor and any subsequent export, see the current_stage
    guard in api/routers/jobs.py). `job` is declared BEFORE the try block so
    the finally block can always reset it even when `session.get` itself
    fails (e.g. a transient DB connection error) — not just on a render
    error. If the render fails (invalid timeline, ffmpeg error, ...),
    `exported_video_gcs_uri` KEEPS its previous value unchanged — we never
    publish a half-baked/broken export; the creator sees the export "revert
    to ready_for_review" and can retry."""
    async with AsyncSessionLocal() as session:
        job: Job | None = None
        # A flag scoped to THIS RUN — MUST NOT be inferred from
        # job.exported_video_gcs_uri (if the job already exported successfully
        # BEFORE, that field still holds its old value even if this retry
        # fails; inferring from it would incorrectly report progress=100 for
        # an export that just failed).
        render_succeeded = False
        try:
            job = await session.get(Job, uuid.UUID(job_id))
            if job is None:
                logger.warning("export: job %s does not exist, skipping", job_id)
                return
            await _update_export_status(
                job_id, stage="exporting", progress=0, target_language=job.target_language
            )

            timeline = TimelineState.model_validate(job.timeline_edits)
            source_video = await session.get(SourceVideo, job.source_video_id)
            lines_result = await session.execute(
                select(DialogueLineRow).where(DialogueLineRow.job_id == job.id)
            )
            dialogue_lines = lines_result.scalars().all()

            with tempfile.TemporaryDirectory(prefix=f"export-{job_id}-") as tmp_dir:
                new_uri = await export_for_job(
                    job, timeline, dialogue_lines, source_video.gcs_uri, _gcs, tmp_dir
                )
            job.exported_video_gcs_uri = new_uri
            render_succeeded = True
        except Exception:  # noqa: BLE001 - ensures current_stage is always reset in finally below
            logger.exception("export: job %s failed while rendering", job_id)
        finally:
            if job is not None:
                job.current_stage = "ready_for_review"
                await session.commit()
                await _update_export_status(
                    job_id,
                    stage="ready_for_review",
                    progress=100 if render_succeeded else None,
                    target_language=job.target_language,
                )


# Above this many seconds of difference between the attached video's real
# (ffprobe-measured) duration and the sum of the job's already-dubbed
# dialogue-line durations, the mismatch is considered worth flagging to the
# creator (US5's edge case: "warn clearly; does not block attaching, but
# does not silently proceed as if nothing were wrong either" — see
# spec.md). Not a hard validation limit — attaching still proceeds and
# mux_service.py's own padding (_pad_video_to_duration) already handles an
# actual video/audio length mismatch gracefully at the ffmpeg level; this is
# purely an advisory signal for the human reviewer, not a safety guard.
_DURATION_MISMATCH_THRESHOLD_SECONDS = 5.0


def _probe_duration(path: str) -> float:
    """Blocking (shells out to ffprobe) — always call via asyncio.to_thread.
    A 4th near-identical copy of this exact one-liner in this codebase (see
    mux_service.py, gemini_client.py, ingestion_agent.py) — an established
    convention of small per-module duration-probing helpers rather than a
    shared utility, kept consistent here rather than introducing a new
    cross-module import for one line."""
    return float(ffmpeg.probe(path)["format"]["duration"])


async def _attach_video(job_id: str) -> None:
    """Handle message type="job_attach_video" (POST /jobs/{id}/attach-video,
    US5, specs/003-script-first-ingestion) — runs ONLY the "muxed" pipeline
    stage directly (Pipeline._run_mux_stage), reusing the dubbed audio
    already produced by every earlier stage; deliberately does NOT re-run
    translation/safety/dubbing — this job already reached ready_for_review
    once, with real dubbed audio, before a video was ever attached (the API
    endpoint already enforces this precondition).

    ALWAYS resets current_stage back to "ready_for_review" whether muxing
    succeeds or fails — same reasoning as _export_job above (otherwise the
    job would be stuck in "muxing" forever, locking out approve_job,
    export_job, and any retry — all 3 already guard on
    current_stage == "ready_for_review")."""
    async with AsyncSessionLocal() as session:
        job: Job | None = None
        mux_succeeded = False
        duration_warning: str | None = None
        try:
            job = await session.get(Job, uuid.UUID(job_id))
            if job is None:
                logger.warning("attach-video: job %s does not exist, skipping", job_id)
                return
            await _update_attach_video_status(
                job_id, stage="muxing", target_language=job.target_language
            )

            source_video = await session.get(SourceVideo, job.source_video_id)
            line_rows = (
                (
                    await session.execute(
                        select(DialogueLineRow).where(DialogueLineRow.job_id == job.id)
                    )
                )
                .scalars()
                .all()
            )
            # What the already-dubbed audio actually expects — ONLY lines
            # that got real audio count (same filter mux_for_job itself uses,
            # see mux_service.py:427-432). Found via code-reviewer, review
            # 2026-10-11: a permanently flagged/undubbed line (allowed to
            # exist forever on a published audio-only job, see
            # approve_job's audio-only branch) contributes NOTHING to the
            # real muxed audio track — if that line happened to be
            # chronologically last, including it here would inflate this
            # proxy past what the real dub ever covers, producing a
            # false-positive mismatch warning with an inaccurate message.
            expected_duration = max(
                (line.end_time for line in line_rows if line.dubbed_audio_gcs_uri), default=0.0
            )

            with tempfile.TemporaryDirectory(prefix=f"attach-video-probe-{job_id}-") as tmp_dir:
                local_video_path = os.path.join(tmp_dir, "attached.mp4")
                await _gcs.download_to_path(source_video.gcs_uri, local_video_path)
                attached_duration = await asyncio.to_thread(_probe_duration, local_video_path)

            if (
                expected_duration > 0
                and abs(attached_duration - expected_duration)
                > _DURATION_MISMATCH_THRESHOLD_SECONDS
            ):
                duration_warning = (
                    f"Attached video is {attached_duration:.1f}s long, but the "
                    f"already-dubbed audio expects about {expected_duration:.1f}s — "
                    "the edit may not match the finished dub."
                )
                logger.warning("job %s: %s", job_id, duration_warning)

            context = await _load_context(job_id)
            context = await _pipeline._run_mux_stage(context)
            job.muxed_video_gcs_uri = context.muxed_video_gcs_uri
            mux_succeeded = True
        except Exception:  # noqa: BLE001 - ensures current_stage is always reset in finally below
            logger.exception("attach-video: job %s failed while muxing", job_id)
        finally:
            if job is not None:
                job.current_stage = "ready_for_review"
                await session.commit()
                await _update_attach_video_status(
                    job_id,
                    stage="ready_for_review",
                    target_language=job.target_language,
                    # Surfaced regardless of mux_succeeded — a duration
                    # mismatch is useful diagnostic context either way, and
                    # arguably MORE useful when muxing just failed (it may
                    # explain why).
                    duration_warning=duration_warning,
                    mux_succeeded=mux_succeeded,
                )


_JOB_LOCK_TTL_SECONDS = 1800


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request):
    _verify_pubsub_token(request)

    envelope = await request.json()
    message_data = envelope["message"]["data"]
    payload = json.loads(base64.b64decode(message_data).decode("utf-8"))

    # Missing `type` (old messages published before this field existed)
    # defaults to "job" for backward compatibility — see
    # api/core/pubsub_client.py.
    message_type = payload.get("type", "job")
    if message_type == "voice_sample":
        await _classify_character_voice_sample(payload["character_id"])
        return {"status": "processed"}

    if message_type == "job_export":
        # A SEPARATE type, does not fall into the "job" branch below —
        # worker/pipeline.py's _STAGE_ORDER has no "exporting" stage, so if it
        # fell through there, _stage_index() would misinterpret it (ValueError
        # -> fallback to index 0) and rerun the ENTIRE pipeline from
        # "transcribed", wasting Gemini quota needlessly and overwriting the
        # existing dialogue_lines/characters. Uses a separate "export:"
        # namespace lock (distinct from the regular "job" lock) for the same
        # reason as the "job" branch below — guarding against duplicate
        # Pub/Sub redelivery.
        job_id = payload["job_id"]
        lock_key = f"pubsub_lock:export:{job_id}"
        if not await _redis.acquire_lock(lock_key, _JOB_LOCK_TTL_SECONDS):
            logger.info(
                "job %s: export is already being processed by another request, skipping", job_id
            )
            return {"status": "skipped_duplicate"}
        try:
            await _export_job(job_id)
        finally:
            await _redis.release_lock(lock_key)
        return {"status": "processed"}

    if message_type == "job_attach_video":
        # A SEPARATE type, same reasoning as "job_export" above — this job
        # is already at "muxing" (not in _STAGE_ORDER), re-running the full
        # pipeline from "transcribed" would re-translate/re-safety-check/
        # re-dub everything that's already done. Uses its own "attach_video:"
        # namespace lock, distinct from both the "job" and "export:" locks.
        job_id = payload["job_id"]
        lock_key = f"pubsub_lock:attach_video:{job_id}"
        if not await _redis.acquire_lock(lock_key, _JOB_LOCK_TTL_SECONDS):
            logger.info(
                "job %s: attach-video is already being processed by another " "request, skipping",
                job_id,
            )
            return {"status": "skipped_duplicate"}
        try:
            await _attach_video(job_id)
        finally:
            await _redis.release_lock(lock_key)
        return {"status": "processed"}

    job_id = payload["job_id"]

    # Pub/Sub redelivers a message when the previous attempt errored/timed out
    # — without a lock, multiple requests for the same job_id would run
    # concurrently, each reading current_stage at the start and checkpointing
    # independently, clobbering each other (reproduced firsthand: current_stage
    # jumping erratically muxed -> safety_checked -> dubbed within seconds
    # because one message got piled up with redeliveries over several days).
    # A request that doesn't win the lock is treated as a duplicate — ack it
    # immediately (return 200) so Pub/Sub stops redelivering THIS message,
    # with no retry/error raised at all; the request holding the lock keeps
    # processing normally.
    lock_key = f"pubsub_lock:{job_id}"
    if not await _redis.acquire_lock(lock_key, _JOB_LOCK_TTL_SECONDS):
        logger.info(
            "job %s: already being processed by another request, skipping duplicate", job_id
        )
        return {"status": "skipped_duplicate"}

    try:
        logger.info("Received job %s", job_id)
        context = await _load_context(job_id)
        # Job is already done (e.g. a late redelivery of a message that was
        # already fully processed before) — "ready_for_review" is not in
        # pipeline.py's _STAGE_ORDER, so if we kept going, _stage_index()
        # would fall into the except branch and rerun the ENTIRE pipeline from
        # scratch (reproduced firsthand: a job already at ready_for_review got
        # rerun from "transcribed" by a late redelivery, wasting Gemini quota
        # needlessly and potentially racing to overwrite a genuinely
        # in-progress job if another job happened to run at the same time).
        # Block it right here, no need to enter the pipeline.
        if context.current_stage == "ready_for_review":
            logger.info("job %s: already ready_for_review, skipping late redelivery", job_id)
            return {"status": "already_done"}
        context, educational_result = await _pipeline.run(
            context, _save_checkpoint, _update_realtime_status
        )
        await _save_educational_result(job_id, educational_result)
    finally:
        await _redis.release_lock(lock_key)

    return {"status": "processed"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
