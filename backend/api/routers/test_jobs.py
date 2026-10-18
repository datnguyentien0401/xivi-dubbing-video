"""Regression test for GET /dialogue-lines/{id}/audio-url — a new endpoint
replacing AudioPlayer.tsx constructing its own
https://storage.googleapis.com/... URL (only works if the bucket is
public-read; the project's real bucket uses uniform bucket-level access,
which is NOT public-read, so that URL returns 403 — manually reproduced while
testing on cloud, see gcs_client.py:generate_signed_read_url).

Needs a real Postgres to test the ownership SQL join (same reason as test_projects.py).

Run: DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
      python -m pytest api/routers/test_jobs.py
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Character, DialogueLine, Job, Project, SourceVideo, User

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="needs a real Postgres to test the SQL join (see docstring)",
)


@pytest_asyncio.fixture
async def owner_and_line(session: AsyncSession):
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()

    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        muxed_video_gcs_uri="gs://bucket/jobs/x/muxed/id.mp4",
    )
    session.add(job)
    await session.flush()

    line = DialogueLine(
        job_id=job.id,
        start_time=0.0,
        end_time=1.0,
        original_text="xin chào",
        dubbed_audio_gcs_uri="gs://bucket/jobs/x/dubbed/line1.wav",
    )
    session.add(line)
    await session.flush()

    return owner, line, job


async def test_get_dialogue_line_audio_url_returns_signed_url(
    session: AsyncSession, owner_and_line, monkeypatch
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, line, _job = owner_and_line
    captured_uri = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured_uri["value"] = gcs_uri
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    response = await jobs_router.get_dialogue_line_audio_url(
        line_id=line.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.url == "https://fake-signed-read-url.test/"
    # Must pass exactly this line's dubbed_audio_gcs_uri, not a hardcoded
    # value or the wrong field.
    assert captured_uri["value"] == line.dubbed_audio_gcs_uri


async def test_get_dialogue_line_audio_url_404_when_no_audio_yet(
    session: AsyncSession, owner_and_line
):
    """A dialogue line that hasn't finished dubbing yet (dubbed_audio_gcs_uri=None)
    — must return a clear 404 instead of calling generate_signed_read_url with None."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, line, _job = owner_and_line
    line.dubbed_audio_gcs_uri = None
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_dialogue_line_audio_url(
            line_id=line.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_get_job_video_url_returns_signed_url(
    session: AsyncSession, owner_and_line, monkeypatch
):
    """Previously there was no endpoint returning the result video —
    ReviewDashboard.tsx only let you listen to audio per line, there was no
    way to view the final published video anywhere (bug fixed)."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    captured_uri = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured_uri["value"] = gcs_uri
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    response = await jobs_router.get_job_video_url(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.url == "https://fake-signed-read-url.test/"
    assert captured_uri["value"] == job.muxed_video_gcs_uri


async def test_get_job_video_url_404_when_not_muxed_yet(session: AsyncSession, owner_and_line):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.muxed_video_gcs_uri = None
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_job_video_url(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_get_job_share_url_409_when_not_published_yet(session: AsyncSession, owner_and_line):
    """The "create share/download video link" feature only opens up AFTER a
    job is published (see AGENTS.md principle #5) — the owner_and_line
    fixture defaults to status="queued" (not approved yet), so this must
    return a clear 409 instead of silently granting a link for an unapproved
    video."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    assert job.status != "published"

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_job_share_url(
            job_id=job.id,
            download=False,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_get_job_share_url_returns_signed_url_when_published(
    session: AsyncSession, owner_and_line, monkeypatch
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.status = "published"
    await session.flush()
    captured = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured["uri"] = gcs_uri
        captured["kwargs"] = kwargs
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    response = await jobs_router.get_job_share_url(
        job_id=job.id,
        download=False,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.url == "https://fake-signed-read-url.test/"
    assert captured["uri"] == job.muxed_video_gcs_uri
    # download=False -> no download filename attached (keeps the "just view
    # the link" behavior, doesn't force the browser to download).
    assert captured["kwargs"]["download_filename"] is None
    # The share link's expiry must be much longer than get_job_video_url (60')
    # — see _SHARE_URL_EXPIRATION_MINUTES.
    assert captured["kwargs"]["expiration_minutes"] == jobs_router._SHARE_URL_EXPIRATION_MINUTES


async def test_get_job_share_url_download_sets_filename_from_project_name(
    session: AsyncSession, owner_and_line, monkeypatch
):
    """download=True must set the download filename from the project name +
    target language (e.g. "p-id.mp4"), instead of letting the browser guess
    the name from the GCS path (e.g. "id.mp4" — unclear which project's video
    it is when a user downloads multiple files)."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.status = "published"
    await session.flush()
    captured = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured["kwargs"] = kwargs
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    await jobs_router.get_job_share_url(
        job_id=job.id,
        download=True,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert captured["kwargs"]["download_filename"] == "p-id.mp4"


@pytest_asyncio.fixture
async def owner_line_and_character(session: AsyncSession):
    """A job in the ready_for_review stage (matching reality when
    ReviewDashboard.tsx calls PATCH /dialogue-lines/{id}) + 1 character in
    the same project, to test changing the voice for a dialogue line."""
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()

    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()

    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()

    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="ready_for_review",
    )
    session.add(job)
    await session.flush()

    character = Character(project_id=project.id, name="Mèo", speaker_tag_original="speaker_1")
    session.add(character)
    await session.flush()

    line = DialogueLine(
        job_id=job.id,
        start_time=0.0,
        end_time=1.0,
        original_text="xin chào",
        translated_text="halo",
        safety_status="ok",
        dubbed_audio_gcs_uri="gs://bucket/jobs/x/dubbed/line1.wav",
    )
    session.add(line)
    await session.flush()

    return owner, job, line, character, project


async def test_update_dialogue_line_reassigns_character_and_triggers_redub(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """The "choose a character voice per line + re-dub" feature: changing
    character_id must (a) save the new character_id, (b) clear the old
    dubbed_audio_gcs_uri (marking it as needing re-dub), (c) move the job
    back to the stage right before "dubbed" and re-publish job_created so
    DubbingAgent re-dubs ONLY this line (see
    worker/pipeline.py::_STAGE_ORDER, DubbingAgent skips lines that already
    have audio)."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, line, character, _project = owner_line_and_character

    background_tasks = BackgroundTasks()
    result = await jobs_router.update_dialogue_line(
        line_id=line.id,
        payload=DialogueLineUpdate(character_id=character.id),
        background_tasks=background_tasks,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.character_id == character.id
    assert result.dubbed_audio_gcs_uri is None
    assert len(background_tasks.tasks) == 1
    await session.refresh(job)
    assert job.current_stage == "ready_for_review"


async def test_update_dialogue_line_404_when_character_belongs_to_other_project(
    session: AsyncSession, owner_line_and_character
):
    """IDOR/data-integrity check: a character belonging to a DIFFERENT project
    (even if the same user owns both projects) must not be assigned to a
    dialogue line in this project — DubbingAgent would not find that
    character in the job's context.characters (context.characters only loads
    by the CORRECT project_id), silently falling back to the default voice
    instead of a clear error if this isn't blocked here."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    owner, _job, line, _character, _project = owner_line_and_character
    other_project = Project(user_id=owner.id, name="p2", source_language="vi")
    session.add(other_project)
    await session.flush()
    other_character = Character(
        project_id=other_project.id, name="Khác project", speaker_tag_original="speaker_1"
    )
    session.add(other_character)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.update_dialogue_line(
            line_id=line.id,
            payload=DialogueLineUpdate(character_id=other_character.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_update_dialogue_line_409_when_job_not_ready_for_review(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    owner, job, line, character, _project = owner_line_and_character
    job.current_stage = "dubbed"  # still processing, hasn't reached review yet
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.update_dialogue_line(
            line_id=line.id,
            payload=DialogueLineUpdate(character_id=character.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_update_dialogue_line_editing_text_on_flagged_line_does_not_wake_worker(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """Regression test for a real UX gap found via feedback: editing the
    translated text of a "Needs Review" (flagged) line used to look like it
    did nothing — the old code ALWAYS republished job_created on any text
    edit, but DubbingAgent skips every line with safety_status != "ok" (see
    dubbing_agent.py) and the pipeline resume path deliberately skips
    re-running SafetyAgent (see worker/pipeline.py::_STAGE_ORDER), so the
    line's flag never cleared and it could never actually get dubbed. The
    fixed behavior: the text IS saved and dubbed_audio_gcs_uri IS cleared,
    but the job must NOT be bumped back to "safety_checked" (no wasted
    worker wakeup) while the line is still flagged — see the companion test
    below for the follow-up PATCH that actually unblocks it."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    owner, job, line, _character, _project = owner_line_and_character
    line.safety_status = "flagged"
    line.dubbed_audio_gcs_uri = None
    await session.flush()

    result = await jobs_router.update_dialogue_line(
        line_id=line.id,
        payload=DialogueLineUpdate(translated_text="fixed text"),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.translated_text == "fixed text"
    assert result.dubbed_audio_gcs_uri is None
    await session.refresh(job)
    assert job.current_stage == "ready_for_review"  # NOT bumped — still flagged


async def test_update_dialogue_line_marking_safe_unblocks_redub(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """The follow-up to the test above: once the text is fixed AND the
    reviewer explicitly marks the line safe (the real human-review step —
    see AGENTS.md principle #5), the worker IS woken up. Also asserts the
    AI's original safety_reasoning gets replaced with a note that this was a
    manual override — SafetyBadge always shows `reasoning` as a tooltip
    (frontend-spec.md section 5), so leaving the OLD AI text there after a
    human override would misrepresent who made the current call."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, line, _character, _project = owner_line_and_character
    line.safety_status = "flagged"
    line.safety_reasoning = "Mentions something inappropriate."
    line.dubbed_audio_gcs_uri = None
    await session.flush()

    result = await jobs_router.update_dialogue_line(
        line_id=line.id,
        payload=DialogueLineUpdate(safety_status="ok"),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.safety_status == "ok"
    assert result.safety_reasoning == "Manually marked safe by the reviewer."
    await session.refresh(job)
    assert job.current_stage == "ready_for_review"  # stays ready_for_review during single-line redub


async def test_update_dialogue_line_combined_text_and_safety_status_in_one_call(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """The realistic single-save workflow: a reviewer fixes the text AND
    marks it safe at once — must apply both and trigger exactly one
    redub (not require 2 separate PATCH calls to converge)."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, line, _character, _project = owner_line_and_character
    line.safety_status = "flagged"
    line.dubbed_audio_gcs_uri = None
    await session.flush()

    result = await jobs_router.update_dialogue_line(
        line_id=line.id,
        payload=DialogueLineUpdate(translated_text="fixed text", safety_status="ok"),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.translated_text == "fixed text"
    assert result.safety_status == "ok"
    await session.refresh(job)
    assert job.current_stage == "ready_for_review"


async def test_update_dialogue_line_reflagging_revokes_existing_audio(
    session: AsyncSession, owner_line_and_character
):
    """A reviewer who decides a previously-"ok" line is actually unsafe must
    not leave its old (already-dubbed) audio sitting around as still
    playable/exportable — re-flagging revokes it. The job must NOT be bumped
    back to "safety_checked" either — there is nothing for the worker to do
    for a newly-flagged line."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    owner, job, line, _character, _project = owner_line_and_character
    assert line.dubbed_audio_gcs_uri is not None  # fixture starts "ok" + dubbed

    result = await jobs_router.update_dialogue_line(
        line_id=line.id,
        payload=DialogueLineUpdate(safety_status="flagged"),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.safety_status == "flagged"
    assert result.dubbed_audio_gcs_uri is None
    assert result.safety_reasoning == "Manually flagged by the reviewer."
    await session.refresh(job)
    assert job.current_stage == "ready_for_review"


async def test_update_dialogue_line_400_when_no_fields_given(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineUpdate

    owner, _job, line, _character, _project = owner_line_and_character

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.update_dialogue_line(
            line_id=line.id,
            payload=DialogueLineUpdate(),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 400


async def test_batch_update_dialogue_lines_success_and_triggers_single_redub(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.db.models import DialogueLine
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineBatchItem, DialogueLinesBatchUpdate

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, line1, character, _project = owner_line_and_character

    # Add a second line on the same job
    line2 = DialogueLine(
        job_id=job.id,
        start_time=3.0,
        end_time=5.0,
        original_text="Second original line",
        translated_text="Old second text",
        safety_status="ok",
        dubbed_audio_gcs_uri="gs://test-bucket/old-audio-2.wav",
    )
    session.add(line2)
    await session.commit()
    await session.refresh(line2)

    payload = DialogueLinesBatchUpdate(
        lines=[
            DialogueLineBatchItem(id=line1.id, translated_text="New line 1 translation"),
            DialogueLineBatchItem(id=line2.id, translated_text="New line 2 translation"),
        ]
    )

    results = await jobs_router.batch_update_dialogue_lines(
        job_id=job.id,
        payload=payload,
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert len(results) == 2
    res_by_id = {r.id: r for r in results}
    assert res_by_id[line1.id].translated_text == "New line 1 translation"
    assert res_by_id[line1.id].dubbed_audio_gcs_uri is None
    assert res_by_id[line2.id].translated_text == "New line 2 translation"
    assert res_by_id[line2.id].dubbed_audio_gcs_uri is None

    await session.refresh(job)
    assert job.current_stage == "safety_checked"


async def test_batch_update_dialogue_lines_409_when_not_ready_for_review(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineBatchItem, DialogueLinesBatchUpdate

    owner, job, line, _character, _project = owner_line_and_character
    job.current_stage = "dubbing"
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.batch_update_dialogue_lines(
            job_id=job.id,
            payload=DialogueLinesBatchUpdate(
                lines=[DialogueLineBatchItem(id=line.id, translated_text="New text")]
            ),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_batch_update_dialogue_lines_404_when_line_not_found(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import DialogueLineBatchItem, DialogueLinesBatchUpdate

    owner, job, _line, _character, _project = owner_line_and_character

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.batch_update_dialogue_lines(
            job_id=job.id,
            payload=DialogueLinesBatchUpdate(
                lines=[DialogueLineBatchItem(id=uuid.uuid4(), translated_text="Ghost line")]
            ),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_list_job_characters_returns_characters_of_same_project(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job, _line, character, _project = owner_line_and_character

    result = await jobs_router.list_job_characters(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert [c.id for c in result] == [character.id]


async def test_get_job_timeline_builds_default_snapshot_from_dialogue_lines(
    session: AsyncSession, owner_line_and_character
):
    """A job whose editor has never been opened (timeline_edits == {} by
    default) — must build its own snapshot: 1 video track + 1
    audio_original track (full video length), and 1 dialogue track for the
    assigned character, see _build_default_timeline."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router

    owner, job, line, character, _project = owner_line_and_character
    line.character_id = character.id
    video = await session.get(SourceVideo, job.source_video_id)
    video.duration_seconds = 90
    await session.flush()

    result = await jobs_router.get_job_timeline(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.duration_seconds == 90
    tracks_by_id = {t.id: t for t in result.tracks}
    assert tracks_by_id["video"].clips[0].source_end == 90
    assert tracks_by_id["audio_original"].clips[0].source_end == 90
    dialogue_track = tracks_by_id[f"character-{character.id}"]
    assert dialogue_track.type == "dialogue"
    assert dialogue_track.character_id == character.id
    assert dialogue_track.clips[0].dialogue_line_id == line.id
    assert dialogue_track.clips[0].timeline_start == line.start_time
    # The audio was already time-stretched by mux_service to fit exactly
    # (end_time - start_time), so the default source_end MUST match that
    # window's length, not the absolute end_time/start_time on the shared
    # timeline.
    assert dialogue_track.clips[0].source_end == line.end_time - line.start_time


async def test_get_job_timeline_excludes_lines_without_character(
    session: AsyncSession, owner_line_and_character
):
    """A dialogue line without an assigned character (character_id NULL) is
    excluded from the default snapshot — an MVP limitation documented in
    _build_default_timeline, this test locks down that this behavior doesn't
    change silently."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router

    owner, job, _line, _character, _project = owner_line_and_character
    video = await session.get(SourceVideo, job.source_video_id)
    video.duration_seconds = 90
    await session.flush()

    result = await jobs_router.get_job_timeline(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert {t.id for t in result.tracks} == {"video", "audio_original"}


async def test_get_job_timeline_returns_saved_snapshot_when_present(
    session: AsyncSession, owner_line_and_character
):
    """If job.timeline_edits already has content (editor was opened and
    autosaved at least once), return exactly the saved content instead of
    rebuilding the default."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job, _line, _character, _project = owner_line_and_character
    saved = {
        "schema_version": 1,
        "duration_seconds": 42.0,
        "tracks": [
            {
                "id": "video",
                "type": "video",
                "clips": [
                    {"id": "v1", "timeline_start": 0.0, "source_start": 0.0, "source_end": 42.0}
                ],
            },
        ],
    }
    job.timeline_edits = saved
    await session.flush()

    result = await jobs_router.get_job_timeline(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.duration_seconds == 42.0
    assert [t.id for t in result.tracks] == ["video"]


async def test_get_job_timeline_409_when_job_not_ready_for_review(
    session: AsyncSession, owner_and_line
):
    """owner_and_line builds a job in the default "queued" stage (not ready for review)."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_job_timeline(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_update_job_timeline_persists_full_snapshot(
    session: AsyncSession, owner_line_and_character
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import Clip, TimelineState, TimelineUpdate, Track

    owner, job, _line, _character, _project = owner_line_and_character
    new_timeline = TimelineState(
        duration_seconds=60.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=60.0)],
            )
        ],
    )

    result = await jobs_router.update_job_timeline(
        job_id=job.id,
        payload=TimelineUpdate(timeline=new_timeline),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.duration_seconds == 60.0
    await session.refresh(job)
    assert job.timeline_edits["duration_seconds"] == 60.0
    assert job.timeline_edits["tracks"][0]["id"] == "video"


async def test_update_job_timeline_409_when_job_not_ready_for_review(
    session: AsyncSession, owner_and_line
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import Clip, TimelineState, TimelineUpdate, Track

    owner, _line, job = owner_and_line
    timeline = TimelineState(
        duration_seconds=10.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=10.0)],
            )
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.update_job_timeline(
            job_id=job.id,
            payload=TimelineUpdate(timeline=timeline),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_get_job_timeline_falls_back_to_last_line_end_time_when_duration_unset(
    session: AsyncSession, owner_line_and_character
):
    """SourceVideo.duration_seconds isn't set yet (e.g. hasn't fully gone
    through Agent 1) — building the default must fall back to the last
    dialogue line instead of blocking entirely."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job, line, _character, _project = owner_line_and_character
    # The fixture doesn't set duration_seconds — defaults to None (a newly
    # created SourceVideo doesn't have it yet), exactly the scenario we need
    # to test here.

    result = await jobs_router.get_job_timeline(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.duration_seconds == line.end_time


async def test_get_job_timeline_409_when_no_duration_and_no_dialogue_lines(
    session: AsyncSession,
):
    """No duration_seconds AND no dialogue_lines at all (an empty job) —
    falls back to 0.0, must return a clear 409 instead of building a
    meaningless empty timeline."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Project, SourceVideo, User
    from api.routers import jobs as jobs_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()
    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="ready_for_review",
    )
    session.add(job)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_job_timeline(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_get_job_timeline_500_when_saved_snapshot_is_invalid(
    session: AsyncSession, owner_line_and_character
):
    """timeline_edits is saved but no longer matches the current TimelineState
    schema (e.g. a future bug/a manual DB edit) — must return a clear 500,
    without exposing the raw ValidationError."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job, _line, _character, _project = owner_line_and_character
    job.timeline_edits = {"not_a_valid_field": True}
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_job_timeline(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 500


@pytest_asyncio.fixture
async def owner_job_two_characters(session: AsyncSession):
    """A job with 2 characters, each with 2 dialogue lines — tests track/clip
    grouping when there are multiple characters (_build_default_timeline must
    split out a separate track per character, keeping clips correctly ordered
    by start_time within each track)."""
    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4", duration_seconds=100)
    session.add(video)
    await session.flush()
    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="ready_for_review",
    )
    session.add(job)
    await session.flush()

    cat = Character(project_id=project.id, name="Mèo", speaker_tag_original="speaker_1")
    dog = Character(project_id=project.id, name="Chó", speaker_tag_original="speaker_2")
    session.add_all([cat, dog])
    await session.flush()

    # Deliberately inserted out of chronological order to confirm the result
    # gets RE-SORTED by start_time within each track, rather than preserving
    # insertion order.
    lines = [
        DialogueLine(
            job_id=job.id,
            character_id=cat.id,
            start_time=10.0,
            end_time=12.0,
            original_text="a",
        ),
        DialogueLine(
            job_id=job.id,
            character_id=dog.id,
            start_time=5.0,
            end_time=7.0,
            original_text="b",
        ),
        DialogueLine(
            job_id=job.id,
            character_id=cat.id,
            start_time=1.0,
            end_time=2.0,
            original_text="c",
        ),
    ]
    session.add_all(lines)
    await session.flush()

    return owner, job, cat, dog, lines


async def test_get_job_timeline_groups_multiple_characters_into_separate_tracks(
    session: AsyncSession, owner_job_two_characters
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job, cat, dog, lines = owner_job_two_characters

    result = await jobs_router.get_job_timeline(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    tracks_by_id = {t.id: t for t in result.tracks}
    assert {f"character-{cat.id}", f"character-{dog.id}"} <= set(tracks_by_id)

    cat_track = tracks_by_id[f"character-{cat.id}"]
    # "Mèo"'s 2 lines (start_time 10.0 and 1.0) — must come out in the
    # CORRECT start_time order (1.0 before 10.0), not DB insertion order.
    assert [c.timeline_start for c in cat_track.clips] == [1.0, 10.0]

    dog_track = tracks_by_id[f"character-{dog.id}"]
    assert [c.timeline_start for c in dog_track.clips] == [5.0]
    assert dog_track.clips[0].dialogue_line_id == lines[1].id


async def test_update_then_get_job_timeline_roundtrip_multi_track(
    session: AsyncSession, owner_line_and_character
):
    """PATCH a multi-track snapshot then GET it back — must return EXACTLY
    what was just saved, without _build_default_timeline overwriting it
    (job.timeline_edits is no longer empty)."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import Clip, TimelineState, TimelineUpdate, Track

    owner, job, line, character, _project = owner_line_and_character
    user = CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email)
    new_timeline = TimelineState(
        duration_seconds=30.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=30.0)],
            ),
            Track(
                id="audio_original",
                type="audio_original",
                clips=[Clip(id="a1", timeline_start=0.0, source_start=0.0, source_end=30.0)],
            ),
            Track(
                id=f"character-{character.id}",
                type="dialogue",
                character_id=character.id,
                muted=True,
                volume=0.5,
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line.id,
                        timeline_start=3.0,
                        source_start=0.0,
                        source_end=1.0,
                    )
                ],
            ),
        ],
    )

    await jobs_router.update_job_timeline(
        job_id=job.id,
        payload=TimelineUpdate(timeline=new_timeline),
        session=session,
        user=user,
    )

    result = await jobs_router.get_job_timeline(job_id=job.id, session=session, user=user)

    assert result.duration_seconds == 30.0
    assert {t.id for t in result.tracks} == {"video", "audio_original", f"character-{character.id}"}
    dialogue_track = next(t for t in result.tracks if t.id == f"character-{character.id}")
    assert dialogue_track.muted is True
    assert dialogue_track.volume == 0.5
    assert dialogue_track.clips[0].timeline_start == 3.0


async def test_export_job_409_when_job_not_ready_for_review(session: AsyncSession, owner_and_line):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line  # current_stage defaults to "queued"

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.export_job(
            job_id=job.id,
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_export_job_409_for_audio_only_job_with_no_video_attached(session: AsyncSession):
    """Regression test (found via code-reviewer, review 2026-10-11): once
    pipeline.py lets a script-first job reach ready_for_review, nothing
    previously stopped POST /export from being called on it — worker/
    main.py::_export_job would then crash with AttributeError dereferencing
    a None SourceVideo. There is no video/timeline to export at all until
    POST /jobs/{id}/attach-video exists and runs."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.export_job(
            job_id=job.id,
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_attach_video_sets_source_video_and_publishes(session: AsyncSession, monkeypatch):
    """specs/003-script-first-ingestion US5 — the happy path: an
    already-dubbed script-first job gets a real video attached, moves to the
    transient "muxing" stage, and schedules a message for the worker."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    video = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/attached.mp4")
    session.add(video)
    await session.flush()
    background_tasks = BackgroundTasks()

    result = await jobs_router.attach_video(
        job_id=job.id,
        payload=AttachVideoRequest(source_video_id=video.id),
        background_tasks=background_tasks,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.source_video_id == video.id
    assert result.current_stage == "muxing"
    assert len(background_tasks.tasks) == 1


async def test_attach_video_409_when_already_successfully_muxed(session: AsyncSession):
    """Blocked only once a video has actually been successfully muxed
    (muxed_video_gcs_uri set) — NOT merely by source_video_id being set (see
    the retry-after-failure test below)."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    already_attached = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/old.mp4")
    session.add(already_attached)
    await session.flush()
    job.source_video_id = already_attached.id
    job.muxed_video_gcs_uri = "gs://bucket/jobs/x/muxed/id.mp4"  # a prior attach SUCCEEDED
    await session.flush()
    new_video = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/new.mp4")
    session.add(new_video)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.attach_video(
            job_id=job.id,
            payload=AttachVideoRequest(source_video_id=new_video.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_attach_video_allows_retry_after_a_failed_attach(session: AsyncSession):
    """Regression test (found via code-reviewer, review 2026-10-11): a
    FAILED attach (muxing failed — source_video_id stays set from the failed
    attempt, current_stage resets to ready_for_review, but
    muxed_video_gcs_uri stays None, see worker/main.py::_attach_video's
    finally block) must NOT permanently block every future attach attempt —
    the old, stricter `source_video_id is not None` guard made a failure
    unrecoverable with no path forward."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    failed_attempt = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/corrupt.mp4")
    session.add(failed_attempt)
    await session.flush()
    job.source_video_id = failed_attempt.id
    job.muxed_video_gcs_uri = None  # the prior mux FAILED, never set
    await session.flush()
    retry_video = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/fixed.mp4")
    session.add(retry_video)
    await session.flush()

    result = await jobs_router.attach_video(
        job_id=job.id,
        payload=AttachVideoRequest(source_video_id=retry_video.id),
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.source_video_id == retry_video.id
    assert result.current_stage == "muxing"


async def test_get_dubbed_audio_bundle_zips_lines_named_by_segment_code(
    session: AsyncSession, monkeypatch
):
    """FR-008 (specs/003-script-first-ingestion) — the downloadable set of
    dubbed audio, named by segment_code so a human editor can match each
    file back to its row in the original script."""
    import io
    import zipfile
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session,
        line_statuses=[
            ("ok", "gs://bucket/dubbed/1.wav"),
            ("ok", "gs://bucket/dubbed/2.wav"),
        ],
    )
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
    lines[0].segment_code = "001"
    lines[1].segment_code = "002"
    await session.flush()

    async def fake_download_to_path(_uri, local_path):
        with open(local_path, "wb") as f:
            f.write(b"fake audio bytes")

    captured: dict = {}

    async def fake_upload_from_path(local_path, blob_name):
        captured["blob_name"] = blob_name
        with open(local_path, "rb") as f:
            captured["zip_bytes"] = f.read()
        return f"gs://bucket/{blob_name}"

    monkeypatch.setattr(jobs_router._storage, "download_to_path", fake_download_to_path)
    monkeypatch.setattr(jobs_router._storage, "upload_from_path", fake_upload_from_path)
    monkeypatch.setattr(
        jobs_router._storage,
        "generate_signed_read_url",
        AsyncMock(return_value="https://fake-signed-read-url.test/"),
    )

    response = await jobs_router.get_dubbed_audio_bundle(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.url == "https://fake-signed-read-url.test/"
    with zipfile.ZipFile(io.BytesIO(captured["zip_bytes"])) as zip_file:
        assert sorted(zip_file.namelist()) == ["001.wav", "002.wav"]


async def test_get_dubbed_audio_bundle_falls_back_to_index_with_no_segment_code(
    session: AsyncSession, monkeypatch
):
    """A video-first job's lines never have a segment_code (only
    create_script_jobs sets it) — must still produce a usable, distinctly
    named bundle rather than erroring or colliding on an empty name."""
    from unittest.mock import AsyncMock

    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session,
        line_statuses=[
            ("ok", "gs://bucket/dubbed/1.wav"),
            ("ok", "gs://bucket/dubbed/2.wav"),
        ],
    )
    # segment_code is left as None (default) — simulating a video-first job.

    async def fake_download_to_path(_uri, local_path):
        with open(local_path, "wb") as f:
            f.write(b"fake audio bytes")

    captured: dict = {}

    async def fake_upload_from_path(local_path, blob_name):
        with open(local_path, "rb") as f:
            captured["zip_bytes"] = f.read()
        return f"gs://bucket/{blob_name}"

    monkeypatch.setattr(jobs_router._storage, "download_to_path", fake_download_to_path)
    monkeypatch.setattr(jobs_router._storage, "upload_from_path", fake_upload_from_path)
    monkeypatch.setattr(
        jobs_router._storage,
        "generate_signed_read_url",
        AsyncMock(return_value="https://fake-signed-read-url.test/"),
    )

    await jobs_router.get_dubbed_audio_bundle(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(captured["zip_bytes"])) as zip_file:
        assert sorted(zip_file.namelist()) == ["line-001.wav", "line-002.wav"]


async def test_get_dubbed_audio_bundle_404_when_no_dubbed_audio_yet(session: AsyncSession):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(session, line_statuses=[("flagged", None)])

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_dubbed_audio_bundle(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_attach_video_409_when_not_ready_for_review(session: AsyncSession):
    from api.core.firebase_auth import CurrentUser
    from api.db.models import SourceVideo
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    job.current_stage = "dubbed"  # still mid-pipeline, not ready_for_review yet
    await session.flush()
    video = SourceVideo(project_id=job.project_id, gcs_uri="gs://bucket/attached.mp4")
    session.add(video)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.attach_video(
            job_id=job.id,
            payload=AttachVideoRequest(source_video_id=video.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_attach_video_404_when_video_belongs_to_a_different_project(
    session: AsyncSession,
):
    """IDOR-style guard: get_owned_source_video only confirms the CALLING
    USER owns the video's project, not that it's the SAME project as the
    job's — without the extra check, a user could attach a video from one
    of their own OTHER projects to this job."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Project, SourceVideo
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    other_project = Project(user_id=owner.id, name="other project", source_language="vi")
    session.add(other_project)
    await session.flush()
    video_in_other_project = SourceVideo(
        project_id=other_project.id, gcs_uri="gs://bucket/other.mp4"
    )
    session.add(video_in_other_project)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.attach_video(
            job_id=job.id,
            payload=AttachVideoRequest(source_video_id=video_in_other_project.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_attach_video_404_when_video_not_owned_by_caller(session: AsyncSession):
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Project, SourceVideo, User
    from api.routers import jobs as jobs_router
    from api.schemas.schemas import AttachVideoRequest

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    stranger = User(
        firebase_uid=f"stranger-{uuid.uuid4()}", email=f"stranger-{uuid.uuid4()}@test.local"
    )
    session.add(stranger)
    await session.flush()
    strangers_project = Project(user_id=stranger.id, name="p2", source_language="vi")
    session.add(strangers_project)
    await session.flush()
    strangers_video = SourceVideo(
        project_id=strangers_project.id, gcs_uri="gs://bucket/strangers.mp4"
    )
    session.add(strangers_video)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.attach_video(
            job_id=job.id,
            payload=AttachVideoRequest(source_video_id=strangers_video.id),
            background_tasks=BackgroundTasks(),
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 404


async def test_export_job_builds_default_timeline_when_never_opened_and_publishes(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """A job whose editor has never been opened (timeline_edits == {} by
    default) — export must (a) build + save a default snapshot before
    publishing, (b) switch current_stage to "exporting", (c) actually
    schedule a message for the worker."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, line, character, _project = owner_line_and_character
    line.character_id = character.id
    await session.flush()
    assert job.timeline_edits == {}

    background_tasks = BackgroundTasks()
    result = await jobs_router.export_job(
        job_id=job.id,
        background_tasks=background_tasks,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.current_stage == "exporting"
    assert len(background_tasks.tasks) == 1
    await session.refresh(job)
    assert job.current_stage == "exporting"
    assert job.timeline_edits != {}
    assert job.timeline_edits["tracks"][0]["id"] == "video"


async def test_export_job_keeps_existing_timeline_edits_unchanged(
    session: AsyncSession, owner_line_and_character, monkeypatch
):
    """A job that ALREADY has timeline_edits (editor was opened + autosaved) —
    export must not silently overwrite it with a new default snapshot."""
    from api.core.config import settings
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    monkeypatch.setattr(settings, "use_local_pubsub", True)
    owner, job, _line, _character, _project = owner_line_and_character
    saved = {
        "schema_version": 1,
        "duration_seconds": 42.0,
        "tracks": [
            {
                "id": "video",
                "type": "video",
                "clips": [
                    {"id": "v1", "timeline_start": 0.0, "source_start": 0.0, "source_end": 42.0}
                ],
            }
        ],
    }
    job.timeline_edits = saved
    await session.flush()

    await jobs_router.export_job(
        job_id=job.id,
        background_tasks=BackgroundTasks(),
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )
    await session.refresh(job)
    assert job.timeline_edits["duration_seconds"] == 42.0


async def test_get_job_video_url_prefers_exported_over_muxed(
    session: AsyncSession, owner_and_line, monkeypatch
):
    """_effective_video_gcs_uri (specs/002-video-editor): once a job has an
    export, GET /jobs/{id}/video-url must return that export, NOT the
    original AI output — the owner_and_line fixture already sets
    muxed_video_gcs_uri, just need to add exported_video_gcs_uri to test the
    correct priority order."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.exported_video_gcs_uri = "gs://bucket/jobs/x/exported/id.mp4"
    await session.flush()
    captured = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured["uri"] = gcs_uri
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    await jobs_router.get_job_video_url(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert captured["uri"] == "gs://bucket/jobs/x/exported/id.mp4"


async def test_get_job_share_url_prefers_exported_over_muxed(
    session: AsyncSession, owner_and_line, monkeypatch
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.status = "published"
    job.exported_video_gcs_uri = "gs://bucket/jobs/x/exported/id.mp4"
    await session.flush()
    captured = {}

    async def fake_generate_signed_read_url(gcs_uri: str, **kwargs):
        captured["uri"] = gcs_uri
        return "https://fake-signed-read-url.test/"

    monkeypatch.setattr(
        jobs_router._storage, "generate_signed_read_url", fake_generate_signed_read_url
    )

    await jobs_router.get_job_share_url(
        job_id=job.id,
        download=False,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert captured["uri"] == "gs://bucket/jobs/x/exported/id.mp4"


async def test_approve_job_409_when_no_video_at_all(session: AsyncSession):
    """Previously approve_job performed no checks at all — it was possible to
    publish a job with no result video whatsoever, leaving the job published
    but with video-url/share-url always returning 404 afterward. Must now be
    clearly blocked with a 409."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Project, SourceVideo, User
    from api.routers import jobs as jobs_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()
    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        current_stage="ready_for_review",
    )  # no result video at all
    session.add(job)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_approve_job_succeeds_with_only_exported_video(session: AsyncSession):
    """A job that has never had a muxed_video_gcs_uri (in theory, the pipeline's
    mux step always produces one, but the guard uses _effective_video_gcs_uri
    so it must still pass when ONLY an export exists) — confirms the guard
    doesn't accidentally require BOTH fields."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import Project, SourceVideo, User
    from api.routers import jobs as jobs_router

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    video = SourceVideo(project_id=project.id, gcs_uri="gs://bucket/src.mp4")
    session.add(video)
    await session.flush()
    job = Job(
        project_id=project.id,
        source_video_id=video.id,
        target_language="id",
        exported_video_gcs_uri="gs://bucket/jobs/x/exported/id.mp4",
        current_stage="ready_for_review",
    )
    session.add(job)
    await session.flush()

    result = await jobs_router.approve_job(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.status == "published"


async def test_approve_job_succeeds_when_both_exported_and_muxed_present(
    session: AsyncSession, owner_and_line
):
    """A job that has BOTH muxed_video_gcs_uri (fixture) and
    exported_video_gcs_uri — the guard only needs at least one of the two, not
    both at the same time."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.exported_video_gcs_uri = "gs://bucket/jobs/x/exported/id.mp4"
    job.current_stage = "ready_for_review"
    await session.flush()

    result = await jobs_router.approve_job(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.status == "published"


async def test_approve_job_409_when_export_in_progress(session: AsyncSession, owner_and_line):
    """A job that is still exporting (current_stage == "exporting", e.g. the
    creator just clicked Export) — must block publishing even though an old
    muxed_video_gcs_uri already exists, to avoid mistakenly publishing the
    original AI output instead of waiting for the new export to finish."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.current_stage = "exporting"
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_approve_job_409_when_redub_in_progress(session: AsyncSession, owner_and_line):
    """Found via code-reviewer, 2026-10-12: the video-attached branch
    previously only blocked current_stage == "exporting" — but editing a
    dialogue line's text/character (update_dialogue_line) rewinds
    current_stage to "safety_checked" and republishes for an automatic
    redub + re-mux, the SAME "stale video" risk as the exporting case (the
    old muxed_video_gcs_uri still points at the pre-edit video for the
    whole duration of that resume). Without this check, a reviewer who
    clicks Approve & Publish during that window would silently publish the
    video WITHOUT their just-made edit, with no error at all — the
    `current_stage != "ready_for_review"` guard (broadened from the
    exporting-only check) closes this the same way for both triggers."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    job.current_stage = "safety_checked"
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def _script_first_job_and_lines(
    session: AsyncSession, *, line_statuses: list[tuple[str, str | None]]
):
    """Builds a script-first job (source_video_id=None) + 1 DialogueLine per
    (safety_status, dubbed_audio_gcs_uri) pair in line_statuses — shared setup
    for the approve_job audio-only branch tests below."""
    from api.db.models import Project, User

    owner = User(firebase_uid=f"owner-{uuid.uuid4()}", email=f"owner-{uuid.uuid4()}@test.local")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="p", source_language="vi")
    session.add(project)
    await session.flush()
    job = Job(
        project_id=project.id,
        source_video_id=None,
        target_language="id",
        current_stage="ready_for_review",
    )
    session.add(job)
    await session.flush()
    for index, (safety_status, dubbed_audio_gcs_uri) in enumerate(line_statuses):
        session.add(
            DialogueLine(
                job_id=job.id,
                start_time=float(index),
                end_time=float(index + 1),
                original_text=f"line {index}",
                safety_status=safety_status,
                dubbed_audio_gcs_uri=dubbed_audio_gcs_uri,
            )
        )
    await session.flush()
    return owner, job


async def test_approve_job_succeeds_for_audio_only_job_with_no_video_at_all(
    session: AsyncSession,
):
    """specs/003-script-first-ingestion US4 — a script-first job must be
    approvable on dubbed audio alone, with zero video ever uploaded."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )

    result = await jobs_router.approve_job(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.status == "published"


async def test_approve_job_succeeds_for_audio_only_job_with_an_intentionally_flagged_line(
    session: AsyncSession,
):
    """A still-flagged line (the reviewer deliberately left it censored,
    never clearing safety_status to "ok") must NOT block approval — DubbingAgent
    never dubs a flagged line by design (see dubbing_agent.py), so requiring
    every line to have audio would make such a job permanently unpublishable."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session,
        line_statuses=[
            ("ok", "gs://bucket/jobs/x/dubbed/line1.wav"),
            ("flagged", None),
        ],
    )

    result = await jobs_router.approve_job(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert result.status == "published"


async def test_approve_job_409_for_audio_only_job_with_no_dubbed_audio_at_all(
    session: AsyncSession,
):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(session, line_statuses=[("flagged", None)])

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_approve_job_409_for_audio_only_job_with_an_ok_line_still_undubbed(
    session: AsyncSession,
):
    """A line that IS expected to be dubbed (safety_status == "ok") but
    hasn't been yet (e.g. still mid-pipeline) must block approval — contrast
    with the "intentionally flagged" case above."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session,
        line_statuses=[
            ("ok", "gs://bucket/jobs/x/dubbed/line1.wav"),
            ("ok", None),
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_approve_job_409_for_audio_only_job_not_ready_for_review(session: AsyncSession):
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, job = await _script_first_job_and_lines(
        session, line_statuses=[("ok", "gs://bucket/jobs/x/dubbed/line1.wav")]
    )
    job.current_stage = "dubbed"
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.approve_job(
            job_id=job.id,
            session=session,
            user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
        )
    assert exc_info.value.status_code == 409


async def test_get_educational_content_returns_vocabulary_and_quiz(
    session: AsyncSession, owner_and_line
):
    """Agent 7 (EducationalAgent) used to generate vocabulary + quiz but only
    vocabulary ever got persisted, and nothing exposed either one through the
    API — this endpoint is what EduPreview.tsx (docs/frontend-spec.md 3.6) reads."""
    from api.core.firebase_auth import CurrentUser
    from api.db.models import QuizItem, VocabularyItem
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line
    session.add(
        VocabularyItem(
            job_id=job.id, term="xin chào", meaning="hello", example_sentence="Xin chào!"
        )
    )
    session.add(
        QuizItem(job_id=job.id, question="What does 'xin chào' mean?", answer="hello")
    )
    await session.flush()

    response = await jobs_router.get_educational_content(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert len(response.vocabulary) == 1
    assert response.vocabulary[0].term == "xin chào"
    assert len(response.quiz) == 1
    assert response.quiz[0].answer == "hello"


async def test_get_educational_content_empty_when_not_generated_yet(
    session: AsyncSession, owner_and_line
):
    """Agent 7 runs off the critical path and never fails the pipeline (see
    worker/agents/educational_agent.py) — a job that hasn't finished
    generating educational content yet (or whose Gemini call errored) must
    return empty lists, not a 404/500."""
    from api.core.firebase_auth import CurrentUser
    from api.routers import jobs as jobs_router

    owner, _line, job = owner_and_line

    response = await jobs_router.get_educational_content(
        job_id=job.id,
        session=session,
        user=CurrentUser(id=owner.id, firebase_uid=owner.firebase_uid, email=owner.email),
    )

    assert response.vocabulary == []
    assert response.quiz == []


async def test_get_educational_content_404_for_other_users_job(
    session: AsyncSession, owner_and_line
):
    from api.core.firebase_auth import CurrentUser
    from api.db.models import User
    from api.routers import jobs as jobs_router

    _owner, _line, job = owner_and_line
    other_user = User(
        firebase_uid=f"other-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@test.local"
    )
    session.add(other_user)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.get_educational_content(
            job_id=job.id,
            session=session,
            user=CurrentUser(
                id=other_user.id, firebase_uid=other_user.firebase_uid, email=other_user.email
            ),
        )
    assert exc_info.value.status_code == 404
