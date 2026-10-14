"""Tests for worker/main.py:_verify_pubsub_token — especially the
settings.use_local_pubsub=True branch (local dev, where no real Pub/Sub push
calls in, so there's no real OIDC token to verify, see
api/core/local_pubsub_client.py).

Importing worker.main doesn't require real credentials (every client I/O is
lazy-init, see worker/services/gcs_client.py, gemini_client.py) — it only
needs Settings' required environment variables (DATABASE_URL,
GOOGLE_CLOUD_PROJECT, REDIS_URL, GCS_BUCKET_NAME, FIREBASE_PROJECT_ID) to
already be set when running tests.
"""

import base64
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import worker.main as worker_main
from worker.main import _verify_pubsub_token, handle_pubsub_push
from worker.models.pipeline_context import PipelineContext


def test_bypass_on_skips_verification_entirely(monkeypatch):
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""  # no Authorization header

    _verify_pubsub_token(fake_request)  # must not raise


def test_bypass_off_still_requires_bearer_token(monkeypatch):
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", False)
    fake_request = MagicMock()
    fake_request.headers.get.return_value = ""

    with pytest.raises(HTTPException) as exc_info:
        _verify_pubsub_token(fake_request)
    assert exc_info.value.status_code == 401


def _fake_context() -> PipelineContext:
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="local://x.mp4",
        source_language="vi",
        target_language="id",
        current_stage="transcribed",
    )


async def test_update_realtime_status_noop_when_local_firestore(monkeypatch):
    """settings.use_local_firestore=True — there's no real Firestore in local
    dev, so it must skip entirely (not even attempt to connect) instead of
    raising."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_firestore", True)
    mock_get_firestore = MagicMock()
    monkeypatch.setattr(worker_main, "_get_firestore", mock_get_firestore)

    await worker_main._update_realtime_status(_fake_context())

    mock_get_firestore.assert_not_called()


async def test_update_realtime_status_swallows_firestore_errors(monkeypatch):
    """Regression test: previously an error writing to Firestore (e.g. a
    local DefaultCredentialsError, or a real network error when deployed)
    would raise out of _update_realtime_status, crashing handle_pubsub_push
    and LOSING all pipeline progress for subsequent stages — even though
    Postgres (_save_checkpoint, the source of truth) had correctly saved the
    current stage. Firestore is only a mirror, so an error here must be
    best-effort (log and continue), never crashing the whole job."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_firestore", False)

    class _FailingDocRef:
        async def set(self, *args, **kwargs):
            raise RuntimeError("Firestore unreachable")

    class _FailingCollection:
        def document(self, _doc_id):
            return _FailingDocRef()

    class _FailingFirestoreClient:
        def collection(self, _name):
            return _FailingCollection()

    monkeypatch.setattr(worker_main, "_get_firestore", lambda: _FailingFirestoreClient())

    await worker_main._update_realtime_status(_fake_context())  # must not raise


def _fake_pubsub_request(job_id: str) -> MagicMock:
    payload = json.dumps({"job_id": job_id}).encode("utf-8")
    envelope = {"message": {"data": base64.b64encode(payload).decode("utf-8")}}
    request = MagicMock()
    request.json = AsyncMock(return_value=envelope)
    request.headers.get.return_value = ""
    return request


async def test_handle_pubsub_push_skips_duplicate_when_lock_not_acquired(monkeypatch):
    """Regression test: Pub/Sub redelivers a message when the previous
    attempt errored/timed out — without a lock keyed on job_id, multiple
    duplicate requests would run the pipeline concurrently, clobbering each
    other's checkpoints (reproduced this firsthand on Cloud Run: one message
    that got piled up with retries over several days made current_stage jump
    erratically muxed -> safety_checked -> dubbed within seconds). A request
    that doesn't win the lock must skip immediately, and must NOT call
    _load_context/pipeline.run."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=False))
    release_mock = AsyncMock()
    monkeypatch.setattr(worker_main._redis, "release_lock", release_mock)
    load_context_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_load_context", load_context_mock)

    response = await handle_pubsub_push(_fake_pubsub_request("job-dup"))

    assert response == {"status": "skipped_duplicate"}
    load_context_mock.assert_not_called()
    release_mock.assert_not_called()  # didn't win the lock, so it isn't ours to release


async def test_handle_pubsub_push_releases_lock_after_processing(monkeypatch):
    """A request that wins the lock must always release it after processing
    (even if it raises) so the next legitimate redelivery (a job that
    genuinely needs a retry) doesn't get blocked by the lock forever."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=True))
    release_mock = AsyncMock()
    monkeypatch.setattr(worker_main._redis, "release_lock", release_mock)
    monkeypatch.setattr(worker_main, "_load_context", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await handle_pubsub_push(_fake_pubsub_request("job-1"))

    release_mock.assert_awaited_once_with("pubsub_lock:job-1")


async def test_handle_pubsub_push_skips_already_done_job(monkeypatch):
    """Regression test: a late redelivery of a job already at
    ready_for_review must not rerun the pipeline — "ready_for_review" is not
    in _STAGE_ORDER (see pipeline.py:_stage_index), so if not blocked here
    the pipeline would rerun from "transcribed", wasting Gemini quota
    needlessly (reproduced this firsthand on Cloud Run)."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=True))
    release_mock = AsyncMock()
    monkeypatch.setattr(worker_main._redis, "release_lock", release_mock)
    monkeypatch.setattr(
        worker_main,
        "_load_context",
        AsyncMock(return_value=_fake_context_with_stage("ready_for_review")),
    )
    pipeline_run_mock = AsyncMock()
    monkeypatch.setattr(worker_main._pipeline, "run", pipeline_run_mock)

    response = await handle_pubsub_push(_fake_pubsub_request("job-done"))

    assert response == {"status": "already_done"}
    pipeline_run_mock.assert_not_called()
    release_mock.assert_awaited_once_with("pubsub_lock:job-done")


def _fake_pubsub_request_payload(payload: dict) -> MagicMock:
    data = json.dumps(payload).encode("utf-8")
    envelope = {"message": {"data": base64.b64encode(data).decode("utf-8")}}
    request = MagicMock()
    request.json = AsyncMock(return_value=envelope)
    request.headers.get.return_value = ""
    return request


async def test_handle_pubsub_push_dispatches_voice_sample_type_without_touching_job_pipeline(
    monkeypatch,
):
    """Regression test for the "add a voice by uploading audio" feature
    (Character Voice Manager): message type="voice_sample" must call
    _classify_character_voice_sample, and must NOT touch _load_context/
    _pipeline.run (reserved for message type="job") — 2 kinds of
    asynchronous work share one topic/subscription, distinguished by the
    `type` field."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    classify_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_classify_character_voice_sample", classify_mock)
    load_context_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_load_context", load_context_mock)

    response = await handle_pubsub_push(
        _fake_pubsub_request_payload({"type": "voice_sample", "character_id": "char-1"})
    )

    assert response == {"status": "processed"}
    classify_mock.assert_awaited_once_with("char-1")
    load_context_mock.assert_not_called()


async def test_handle_pubsub_push_defaults_missing_type_to_job(monkeypatch):
    """An old message (published BEFORE the `type` field existed, shaped
    like {"job_id": ...} with no `type` field) must still be processed as
    "job" — so as not to break messages already sitting in Pub/Sub at the
    moment this version adding the `type` field is deployed."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(worker_main._redis, "release_lock", AsyncMock())
    load_context_mock = AsyncMock(return_value=_fake_context_with_stage("ready_for_review"))
    monkeypatch.setattr(worker_main, "_load_context", load_context_mock)
    classify_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_classify_character_voice_sample", classify_mock)

    # OLD payload — no "type" field, only "job_id" (the exact format before
    # message type="voice_sample" was added).
    response = await handle_pubsub_push(_fake_pubsub_request_payload({"job_id": "job-legacy"}))

    assert response == {"status": "already_done"}
    load_context_mock.assert_awaited_once_with("job-legacy")
    classify_mock.assert_not_called()


async def test_handle_pubsub_push_dispatches_job_export_type_without_touching_job_pipeline(
    monkeypatch,
):
    """specs/002-video-editor: message type="job_export" (POST
    /jobs/{id}/export) must call _export_job through a SEPARATE lock
    namespace ("pubsub_lock:export:..."), and must NOT fall into the default
    "job" branch — _STAGE_ORDER has no "exporting" stage, so if it fell
    through there, _stage_index() would fall back to 0 and rerun the ENTIRE
    pipeline from "transcribed" (see worker/pipeline.py, the same bug family
    that test_handle_pubsub_push_skips_already_done_job already had to patch
    for the "ready_for_review" stage)."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=True))
    release_mock = AsyncMock()
    monkeypatch.setattr(worker_main._redis, "release_lock", release_mock)
    export_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_export_job", export_mock)
    load_context_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_load_context", load_context_mock)

    response = await handle_pubsub_push(
        _fake_pubsub_request_payload({"type": "job_export", "job_id": "job-export-1"})
    )

    assert response == {"status": "processed"}
    export_mock.assert_awaited_once_with("job-export-1")
    load_context_mock.assert_not_called()
    release_mock.assert_awaited_once_with("pubsub_lock:export:job-export-1")


async def test_handle_pubsub_push_skips_duplicate_job_export(monkeypatch):
    """Duplicate redelivery of an export message — same dedup reasoning as the
    "job" branch (see
    test_handle_pubsub_push_skips_duplicate_when_lock_not_acquired), using a
    separate lock namespace so it doesn't get mixed up with a regular job's
    lock."""
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    monkeypatch.setattr(worker_main._redis, "acquire_lock", AsyncMock(return_value=False))
    release_mock = AsyncMock()
    monkeypatch.setattr(worker_main._redis, "release_lock", release_mock)
    export_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_export_job", export_mock)

    response = await handle_pubsub_push(
        _fake_pubsub_request_payload({"type": "job_export", "job_id": "job-export-dup"})
    )

    assert response == {"status": "skipped_duplicate"}
    export_mock.assert_not_called()
    release_mock.assert_not_called()


_HAS_DATABASE_URL = "DATABASE_URL" in os.environ


async def test_classify_character_voice_sample_updates_voice_profile_metadata(
    monkeypatch, worker_session_isolation
):
    """Regression test for the "add a voice by uploading audio" feature:
    after the user uploads a voice sample for a Character
    (voice_sample_gcs_uri already set, voice_profile_metadata still empty),
    the worker must listen to the sample and save the chosen voice_name —
    sharing logic/fallback with VoiceProfilingAgent (see
    worker/agents/voice_profiling_agent.py::build_voice_profile_metadata).

    Uses the `worker_session_isolation` fixture (see conftest.py) instead of
    opening its own `AsyncSessionLocal()` — that fixture joins the SAME
    transaction savepoint as the session that
    `worker_main._classify_character_voice_sample` creates internally, so the
    test data (owner/project/character) does NOT leak into the real dev DB no
    matter how many times the function under test calls `commit()`."""
    from api.db.models import Character as CharacterRow
    from api.db.models import Project, User

    session = worker_session_isolation
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    character = CharacterRow(
        project_id=project.id,
        name="Giọng mới",
        speaker_tag_original=f"manual_{uuid.uuid4()}",
        voice_sample_gcs_uri="gs://bucket/manual-sample.wav",
    )
    session.add(character)
    await session.commit()
    character_id = character.id

    monkeypatch.setattr(worker_main._gcs, "download_to_path", AsyncMock())
    # Voice Design (priority layer 1) simulates an explicit error to fall
    # through to select_voice_for_speaker (layer 2) — without this mock it
    # would call the real API with a test/empty key, failing for real through
    # _DEFAULT_RETRIES=6 backoff rounds (costing dozens of seconds per test,
    # not deterministic).
    monkeypatch.setattr(
        worker_main._gemini,
        "describe_voice_from_sample",
        AsyncMock(side_effect=RuntimeError("Voice Design failed (test simulation)")),
    )
    monkeypatch.setattr(
        worker_main._gemini,
        "select_voice_for_speaker",
        AsyncMock(
            return_value={
                "voice_name": "Kore",
                "style_description": "nữ, ấm",
                "reasoning": "Chọn giọng Kore vì mẫu giọng nghe nữ tính, ấm áp.",
            }
        ),
    )

    await worker_main._classify_character_voice_sample(str(character_id))

    # expire_all() is REQUIRED before reading again — `character` is still
    # alive (a local variable, a strong reference) throughout this function,
    # so THIS `session`'s identity map still holds onto the OLD object (from
    # before the update); _classify_character_voice_sample modifies/commits
    # through a DIFFERENT session (even though it shares the same
    # connection/savepoint), so it doesn't automatically sync back into this
    # session — without expiring, session.get() would just return the stale
    # cached object instead of re-querying the DB (a real bug encountered
    # while writing this test).
    session.expire_all()
    updated = await session.get(CharacterRow, character_id)
    assert updated.voice_profile_metadata["voice_name"] == "Kore"
    # style_description must take the correct short field, and must NOT
    # mistakenly take `reasoning` (the long explanation) — see
    # voice_profiling_agent.py::build_voice_profile_metadata.
    assert updated.voice_profile_metadata["style_description"] == "nữ, ấm"


async def test_classify_character_voice_sample_skips_when_no_sample_uploaded_yet(
    monkeypatch, worker_session_isolation
):
    """A Character with no voice_sample_gcs_uri yet (e.g. a message redelivered
    late after the user already deleted it/hasn't finished uploading) — must
    be skipped, without calling Gemini."""
    from api.db.models import Character as CharacterRow
    from api.db.models import Project, User

    session = worker_session_isolation
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    character = CharacterRow(
        project_id=project.id, name="Chưa có mẫu", speaker_tag_original=f"manual_{uuid.uuid4()}"
    )
    session.add(character)
    await session.commit()
    character_id = character.id

    select_voice_mock = AsyncMock()
    monkeypatch.setattr(worker_main._gemini, "select_voice_for_speaker", select_voice_mock)

    await worker_main._classify_character_voice_sample(str(character_id))  # must not raise

    select_voice_mock.assert_not_called()


_VALID_TIMELINE_EDITS = {
    "schema_version": 1,
    "duration_seconds": 2.0,
    "tracks": [
        {
            "id": "video",
            "type": "video",
            "clips": [{"id": "v1", "timeline_start": 0.0, "source_start": 0.0, "source_end": 2.0}],
        }
    ],
}


async def _make_job_ready_for_export(
    session,
    *,
    with_timeline: bool = True,
    exported_video_gcs_uri: str | None = None,
) -> uuid.UUID:
    from api.db.models import Job as JobRow
    from api.db.models import Project, SourceVideo, User

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()
    job = JobRow(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="exporting",
        timeline_edits=_VALID_TIMELINE_EDITS if with_timeline else {},
        exported_video_gcs_uri=exported_video_gcs_uri,
    )
    session.add(job)
    await session.commit()
    return job.id


async def test_export_job_calls_export_for_job_and_saves_result(
    monkeypatch, worker_session_isolation
):
    """_export_job (after replacing the placeholder) must parse
    `timeline_edits` into a TimelineState, call the real
    editor_export_service.export_for_job, save the result to
    exported_video_gcs_uri, and ALWAYS reset current_stage back to
    ready_for_review."""
    from api.db.models import Job as JobRow

    session = worker_session_isolation
    job_id = await _make_job_ready_for_export(session)
    export_mock = AsyncMock(return_value="gs://bucket/jobs/x/exported/id.mp4")
    monkeypatch.setattr(worker_main, "export_for_job", export_mock)

    await worker_main._export_job(str(job_id))

    export_mock.assert_awaited_once()
    # expire_all() before reading again — same reasoning as
    # test_classify_character_voice_sample_updates_voice_profile_metadata
    # (`session`'s identity map doesn't automatically know about a DIFFERENT
    # session, even sharing the same connection/savepoint, that just
    # modified/committed the same row).
    session.expire_all()
    updated = await session.get(JobRow, job_id)
    assert updated.exported_video_gcs_uri == "gs://bucket/jobs/x/exported/id.mp4"
    assert updated.current_stage == "ready_for_review"


async def test_export_job_resets_stage_when_render_fails(monkeypatch, worker_session_isolation):
    """export_for_job fails (e.g. an ffmpeg error) — exported_video_gcs_uri
    KEEPS its previous value (never publishing a broken/half-baked result),
    but current_stage must still be reset to ready_for_review, never stuck
    in "exporting" forever."""
    from api.db.models import Job as JobRow

    session = worker_session_isolation
    job_id = await _make_job_ready_for_export(session)
    monkeypatch.setattr(
        worker_main, "export_for_job", AsyncMock(side_effect=RuntimeError("ffmpeg boom"))
    )

    await worker_main._export_job(str(job_id))  # must not raise

    session.expire_all()  # see the comment on the successful-export test above
    updated = await session.get(JobRow, job_id)
    assert updated.exported_video_gcs_uri is None
    assert updated.current_stage == "ready_for_review"


async def test_export_job_reports_failure_progress_even_after_prior_successful_export(
    monkeypatch, worker_session_isolation
):
    """Regression test (discovered during review): a job ALREADY has an
    exported_video_gcs_uri from a previous successful export; this NEW
    export attempt fails — that field correctly keeps its old value
    (correctly not overwritten by a broken result), but
    _update_export_status must NOT infer "progress=100" just because that
    field happens to still hold a value from before — it must correctly
    report progress=None (error) for THIS RUN."""
    job_id = await _make_job_ready_for_export(
        worker_session_isolation, exported_video_gcs_uri="gs://bucket/jobs/x/exported/old.mp4"
    )
    monkeypatch.setattr(
        worker_main, "export_for_job", AsyncMock(side_effect=RuntimeError("ffmpeg boom"))
    )
    status_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_update_export_status", status_mock)

    await worker_main._export_job(str(job_id))  # must not raise

    final_call = status_mock.await_args_list[-1]
    assert final_call.kwargs["stage"] == "ready_for_review"
    assert final_call.kwargs["progress"] is None


async def test_export_job_resets_stage_when_timeline_edits_invalid(
    monkeypatch, worker_session_isolation
):
    """timeline_edits is still the sentinel {} (guarding against anomalous
    data — the normal POST /jobs/{id}/export flow always guarantees it's
    initialized before publishing the message) — TimelineState.model_validate
    raises, export_for_job must NOT be called, but current_stage must still
    be reset to ready_for_review."""
    from api.db.models import Job as JobRow

    session = worker_session_isolation
    job_id = await _make_job_ready_for_export(session, with_timeline=False)
    export_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "export_for_job", export_mock)

    await worker_main._export_job(str(job_id))  # must not raise

    export_mock.assert_not_called()
    session.expire_all()  # see the comment on the successful-export test above
    updated = await session.get(JobRow, job_id)
    assert updated.exported_video_gcs_uri is None
    assert updated.current_stage == "ready_for_review"


@pytest.mark.skipif(
    not _HAS_DATABASE_URL, reason="requires a real Postgres to test against a real DB"
)
async def test_export_job_logs_and_returns_when_job_not_found():
    """job_id doesn't exist (e.g. the job was deleted between publishing and
    the worker processing it) — must skip quietly, without raising (there's
    no job to reset the stage on). Doesn't need `worker_session_isolation` —
    this test creates no data at all."""
    await worker_main._export_job(str(uuid.uuid4()))  # must not raise


async def _make_job_ready_for_attach_video(
    session, *, line_end_times=(5.0,), trailing_flagged_line_end_time: float | None = None
) -> uuid.UUID:
    """A script-first job (specs/003-script-first-ingestion/US5) that just
    had a video attached by the API endpoint — source_video_id already set,
    current_stage already "muxing", with dubbed dialogue lines whose
    end_time values determine the "expected duration" _attach_video compares
    the attached video's real probed duration against.

    `trailing_flagged_line_end_time`, if given, adds ONE extra line with
    safety_status="flagged" and no dubbed_audio_gcs_uri at a LATER end_time
    than every dubbed line — reproduces the real scenario from a prior
    code-reviewer finding: a permanently flagged/undubbed line (allowed to
    exist forever on a published audio-only job, see approve_job's
    audio-only branch) must NOT count toward the "expected duration" proxy,
    since it contributes nothing to the real muxed audio track."""
    from api.db.models import DialogueLine as DialogueLineRow
    from api.db.models import Job as JobRow
    from api.db.models import Project, SourceVideo, User

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/attached.mp4")
    session.add(video)
    await session.flush()
    job = JobRow(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="muxing",
    )
    session.add(job)
    await session.flush()
    for index, end_time in enumerate(line_end_times):
        session.add(
            DialogueLineRow(
                job_id=job.id,
                start_time=max(0.0, end_time - 1.0),
                end_time=end_time,
                original_text=f"line {index}",
                safety_status="ok",
                dubbed_audio_gcs_uri=f"gs://bucket/dubbed/{index}.wav",
            )
        )
    if trailing_flagged_line_end_time is not None:
        session.add(
            DialogueLineRow(
                job_id=job.id,
                start_time=max(0.0, trailing_flagged_line_end_time - 1.0),
                end_time=trailing_flagged_line_end_time,
                original_text="flagged line",
                safety_status="flagged",
                dubbed_audio_gcs_uri=None,
            )
        )
    await session.commit()
    return job.id


def _patch_attach_video_collaborators(monkeypatch, *, probed_duration: float, mux_result):
    """Mocks every external collaborator _attach_video calls, so these tests
    exercise only its own orchestration logic — no real ffmpeg/GCS/Gemini.
    Also patches Pipeline.run (the FULL stage loop) so tests can assert it
    was never called — _attach_video's core guarantee (US5/FR-009) is that
    attaching a video does NOT re-trigger translation/safety/dubbing, not
    just that _run_mux_stage happens to get called."""
    monkeypatch.setattr(worker_main._gcs, "download_to_path", AsyncMock())
    monkeypatch.setattr(worker_main, "_probe_duration", MagicMock(return_value=probed_duration))
    monkeypatch.setattr(
        worker_main, "_load_context", AsyncMock(return_value=_fake_context_with_stage("dubbed"))
    )
    run_mux_stage_mock = AsyncMock(side_effect=mux_result)
    monkeypatch.setattr(worker_main._pipeline, "_run_mux_stage", run_mux_stage_mock)
    pipeline_run_mock = AsyncMock()
    monkeypatch.setattr(worker_main._pipeline, "run", pipeline_run_mock)
    return run_mux_stage_mock, pipeline_run_mock


async def test_attach_video_runs_mux_stage_and_saves_result(monkeypatch, worker_session_isolation):
    """_attach_video must call Pipeline._run_mux_stage directly (not the
    full pipeline run), save the result to muxed_video_gcs_uri, and reset
    current_stage back to ready_for_review."""
    from api.db.models import Job as JobRow

    session = worker_session_isolation
    job_id = await _make_job_ready_for_attach_video(session, line_end_times=(5.0,))

    async def mux_result(context):
        context.muxed_video_gcs_uri = "gs://bucket/jobs/x/muxed/id.mp4"
        return context

    run_mux_stage_mock, pipeline_run_mock = _patch_attach_video_collaborators(
        monkeypatch, probed_duration=5.1, mux_result=mux_result
    )

    await worker_main._attach_video(str(job_id))

    run_mux_stage_mock.assert_awaited_once()
    # The core guarantee (US5/FR-009): attaching a video must NEVER re-run
    # translation/safety/dubbing — only ever the mux stage directly.
    pipeline_run_mock.assert_not_called()
    session.expire_all()  # see the equivalent comment on the export tests above
    updated = await session.get(JobRow, job_id)
    assert updated.muxed_video_gcs_uri == "gs://bucket/jobs/x/muxed/id.mp4"
    assert updated.current_stage == "ready_for_review"


async def test_attach_video_resets_stage_when_mux_fails(monkeypatch, worker_session_isolation):
    """Pipeline._run_mux_stage fails (e.g. an ffmpeg error) —
    muxed_video_gcs_uri stays None (never publishing a broken result), but
    current_stage must still be reset to ready_for_review, never stuck in
    "muxing" forever."""
    from api.db.models import Job as JobRow

    session = worker_session_isolation
    job_id = await _make_job_ready_for_attach_video(session)
    _patch_attach_video_collaborators(
        monkeypatch, probed_duration=5.1, mux_result=RuntimeError("ffmpeg boom")
    )

    await worker_main._attach_video(str(job_id))  # must not raise

    session.expire_all()
    updated = await session.get(JobRow, job_id)
    assert updated.muxed_video_gcs_uri is None
    assert updated.current_stage == "ready_for_review"


async def test_attach_video_warns_on_duration_mismatch(monkeypatch, worker_session_isolation):
    """The attached video's real probed duration (50s) is far from what the
    already-dubbed audio expects (5s, from the lines' own end_time) — must
    surface a clear warning, not silently proceed as if nothing were
    wrong (US5's edge case in spec.md)."""
    session = worker_session_isolation
    job_id = await _make_job_ready_for_attach_video(session, line_end_times=(5.0,))

    async def mux_result(context):
        context.muxed_video_gcs_uri = "gs://bucket/jobs/x/muxed/id.mp4"
        return context

    _patch_attach_video_collaborators(monkeypatch, probed_duration=50.0, mux_result=mux_result)
    status_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_update_attach_video_status", status_mock)

    await worker_main._attach_video(str(job_id))

    final_call = status_mock.await_args_list[-1]
    assert final_call.kwargs["duration_warning"] is not None
    assert "50.0" in final_call.kwargs["duration_warning"]


async def test_attach_video_no_warning_when_duration_is_close_enough(
    monkeypatch, worker_session_isolation
):
    session = worker_session_isolation
    job_id = await _make_job_ready_for_attach_video(session, line_end_times=(5.0,))

    async def mux_result(context):
        context.muxed_video_gcs_uri = "gs://bucket/jobs/x/muxed/id.mp4"
        return context

    # 5.1s vs an expected 5.0s — well within _DURATION_MISMATCH_THRESHOLD_SECONDS.
    _patch_attach_video_collaborators(monkeypatch, probed_duration=5.1, mux_result=mux_result)
    status_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_update_attach_video_status", status_mock)

    await worker_main._attach_video(str(job_id))

    final_call = status_mock.await_args_list[-1]
    assert final_call.kwargs["duration_warning"] is None


async def test_attach_video_ignores_a_trailing_flagged_undubbed_line_in_duration_check(
    monkeypatch, worker_session_isolation
):
    """Regression test (found via code-reviewer, review 2026-10-11): a
    permanently flagged/undubbed line is allowed to exist forever on a
    published audio-only job (see approve_job's audio-only branch) — it
    contributes NOTHING to the real muxed audio track (mux_for_job only
    places audio for lines with dubbed_audio_gcs_uri set). If that line is
    chronologically LAST, it must NOT inflate the "expected duration" proxy
    — a correctly-sized attached video (matching the real dubbed content)
    must not trigger a false-positive mismatch warning just because a
    flagged line's placeholder timestamp extends further."""
    session = worker_session_isolation
    # Dubbed content ends at 5.0s; the flagged line's own timestamp claims
    # the "script" runs to 50.0s — the attached video correctly matches the
    # REAL dubbed content (5.0s), not the flagged line's irrelevant timestamp.
    job_id = await _make_job_ready_for_attach_video(
        session, line_end_times=(5.0,), trailing_flagged_line_end_time=50.0
    )

    async def mux_result(context):
        context.muxed_video_gcs_uri = "gs://bucket/jobs/x/muxed/id.mp4"
        return context

    _patch_attach_video_collaborators(monkeypatch, probed_duration=5.1, mux_result=mux_result)
    status_mock = AsyncMock()
    monkeypatch.setattr(worker_main, "_update_attach_video_status", status_mock)

    await worker_main._attach_video(str(job_id))

    final_call = status_mock.await_args_list[-1]
    assert final_call.kwargs["duration_warning"] is None


@pytest.mark.skipif(
    not _HAS_DATABASE_URL, reason="requires a real Postgres to test against a real DB"
)
async def test_attach_video_logs_and_returns_when_job_not_found():
    """Mirrors test_export_job_logs_and_returns_when_job_not_found — no job
    to reset the stage on, must skip quietly without raising."""
    await worker_main._attach_video(str(uuid.uuid4()))  # must not raise


def _fake_context_with_stage(stage: str) -> PipelineContext:
    return PipelineContext(
        job_id="job-done",
        project_id="proj-1",
        source_video_gcs_uri="local://x.mp4",
        source_language="vi",
        target_language="id",
        current_stage=stage,
    )
