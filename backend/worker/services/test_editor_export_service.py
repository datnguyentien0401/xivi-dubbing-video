"""Tests for editor_export_service.py (Video Editor, specs/002-video-editor).

Split into 2 groups: (1) pure logic (mute/solo, resolving the source asset,
download/upload in export_for_job) — no real ffmpeg needed, render_export is
mocked/patched; (2) real rendering through ffmpeg (_HAS_FFMPEG) — builds fake
video/audio with lavfi, verifies the output via ffprobe, same style as
test_mux_service.py.
"""

import shutil
import subprocess
import uuid
import wave
from unittest.mock import AsyncMock, patch

import pytest

import worker.services.editor_export_service as editor_export_service
from api.db.models import DialogueLine, Job
from api.schemas.schemas import Clip, TimelineState, Track
from worker.services.editor_export_service import (
    _build_silent_video,
    _resolve_clip_audio_source,
    _select_audible_tracks,
    export_for_job,
    render_export,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --- _select_audible_tracks (pure, no ffmpeg needed) ---


def _track(id_: str, *, muted: bool = False, solo: bool = False) -> Track:
    return Track(id=id_, type="audio_original", muted=muted, solo=solo)


def test_select_audible_tracks_returns_all_unmuted_when_no_solo():
    tracks = [_track("a"), _track("b", muted=True), _track("c")]
    assert [t.id for t in _select_audible_tracks(tracks)] == ["a", "c"]


def test_select_audible_tracks_only_solo_when_one_is_soloed():
    tracks = [_track("a"), _track("b", solo=True), _track("c")]
    assert [t.id for t in _select_audible_tracks(tracks)] == ["b"]


def test_select_audible_tracks_mute_wins_over_solo_on_same_track():
    """A track that is both solo and muted — mute wins, it's excluded even
    though it's soloed (standard DAW behavior), not the other way around
    (solo overriding mute)."""
    tracks = [_track("a", solo=True, muted=True), _track("b")]
    assert [t.id for t in _select_audible_tracks(tracks)] == []


def test_select_audible_tracks_empty_list_returns_empty():
    assert _select_audible_tracks([]) == []


# --- _resolve_clip_audio_source (pure) ---


def test_resolve_clip_audio_source_dialogue_track_returns_mapped_path():
    line_id = uuid.uuid4()
    clip = Clip(
        id="c1", dialogue_line_id=line_id, timeline_start=0.0, source_start=0.0, source_end=1.0
    )
    track = Track(id="t", type="dialogue", character_id=uuid.uuid4())
    paths = {line_id: "/tmp/line.wav"}

    assert _resolve_clip_audio_source(clip, track, "/tmp/source.mp4", paths) == "/tmp/line.wav"


def test_resolve_clip_audio_source_dialogue_track_returns_none_when_not_downloaded():
    """A dialogue line referenced by a clip but not present in
    dialogue_audio_paths (e.g. that line hasn't been dubbed yet, so
    export_for_job didn't download it) — returns None, does NOT raise, so
    _build_mixed_audio can skip it on its own (keeping silence there)."""
    line_id = uuid.uuid4()
    clip = Clip(
        id="c1", dialogue_line_id=line_id, timeline_start=0.0, source_start=0.0, source_end=1.0
    )
    track = Track(id="t", type="dialogue", character_id=uuid.uuid4())

    assert _resolve_clip_audio_source(clip, track, "/tmp/source.mp4", {}) is None


def test_resolve_clip_audio_source_audio_original_track_uses_video_source():
    clip = Clip(id="c1", timeline_start=0.0, source_start=0.0, source_end=1.0)
    track = Track(id="audio_original", type="audio_original")

    assert _resolve_clip_audio_source(clip, track, "/tmp/source.mp4", {}) == "/tmp/source.mp4"


# --- export_for_job (download/upload orchestration, render_export patched out) ---


def _job_and_lines() -> tuple[Job, list[DialogueLine]]:
    job = Job(id=uuid.uuid4(), target_language="id")
    referenced = DialogueLine(
        id=uuid.uuid4(),
        job_id=job.id,
        start_time=0.0,
        end_time=1.0,
        original_text="a",
        dubbed_audio_gcs_uri="gs://bucket/jobs/x/dubbed/line1.wav",
    )
    not_referenced = DialogueLine(
        id=uuid.uuid4(),
        job_id=job.id,
        start_time=1.0,
        end_time=2.0,
        original_text="b",
        dubbed_audio_gcs_uri="gs://bucket/jobs/x/dubbed/line2.wav",
    )
    not_dubbed_yet = DialogueLine(
        id=uuid.uuid4(),
        job_id=job.id,
        start_time=2.0,
        end_time=3.0,
        original_text="c",
    )
    return job, [referenced, not_referenced, not_dubbed_yet]


def _timeline_referencing(line_id: uuid.UUID) -> TimelineState:
    return TimelineState(
        duration_seconds=5.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=5.0)],
            ),
            Track(
                id="character-x",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line_id,
                        timeline_start=0.0,
                        source_start=0.0,
                        source_end=1.0,
                    )
                ],
            ),
        ],
    )


async def test_export_for_job_downloads_only_referenced_and_dubbed_lines(tmp_path):
    job, lines = _job_and_lines()
    referenced_line = lines[0]
    timeline = _timeline_referencing(referenced_line.id)

    mock_gcs = AsyncMock()
    mock_gcs.upload_from_path.return_value = "gs://bucket/jobs/x/exported/id.mp4"

    with patch("worker.services.editor_export_service.render_export") as mock_render:
        result = await export_for_job(
            job, timeline, lines, "gs://bucket/src.mp4", mock_gcs, str(tmp_path)
        )

    # 1 call for the source video + 1 call for EXACTLY the referenced line
    # (doesn't download a line no clip uses, doesn't download a line not
    # dubbed yet).
    assert mock_gcs.download_to_path.await_count == 2
    downloaded_uris = [call.args[0] for call in mock_gcs.download_to_path.await_args_list]
    assert "gs://bucket/src.mp4" in downloaded_uris
    assert referenced_line.dubbed_audio_gcs_uri in downloaded_uris

    mock_render.assert_called_once()
    dialogue_audio_paths_arg = mock_render.call_args[0][2]
    assert set(dialogue_audio_paths_arg.keys()) == {referenced_line.id}

    mock_gcs.upload_from_path.assert_awaited_once()
    assert result == "gs://bucket/jobs/x/exported/id.mp4"


async def test_export_for_job_uses_new_blob_path_each_time(tmp_path):
    """Same reason as mux_for_job (browser caching) — each export must
    produce a NEW blob path, not overwrite the same old path."""
    job, lines = _job_and_lines()
    timeline = _timeline_referencing(lines[0].id)
    mock_gcs = AsyncMock()
    captured_blob_names: list[str] = []

    async def fake_upload(_local_path, blob_name):
        captured_blob_names.append(blob_name)
        return f"gs://bucket/{blob_name}"

    mock_gcs.upload_from_path.side_effect = fake_upload

    with patch("worker.services.editor_export_service.render_export"):
        await export_for_job(
            job, timeline, lines, "gs://bucket/src.mp4", mock_gcs, str(tmp_path / "run1")
        )
        await export_for_job(
            job, timeline, lines, "gs://bucket/src.mp4", mock_gcs, str(tmp_path / "run2")
        )

    assert len(captured_blob_names) == 2
    assert captured_blob_names[0] != captured_blob_names[1]
    for name in captured_blob_names:
        assert name.startswith(f"jobs/{job.id}/exported/id")
        assert name.endswith(".mp4")


# --- render_export: real ffmpeg ---


def _make_test_video(path: str, duration: float, color: str = "red") -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x64:d={duration}:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            path,
        ],
        check=True,
        capture_output=True,
    )


def _write_silent_wav(path: str, duration_seconds: float, framerate: int = 24000) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00\x00" * int(duration_seconds * framerate))


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _probe_stream_duration(path: str, codec_type: str) -> float:
    """The duration of JUST the video/audio stream — unlike _probe_duration
    (which reads `format=duration`, usually reflecting the LONGEST stream in
    the MP4 container), because this is the exact measurement that catches
    an audio/video sync bug between the 2 tracks (see _probe_duration in
    editor_export_service.py)."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v" if codec_type == "video" else "a",
            "-show_entries",
            "stream=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _has_stream(path: str, codec_type: str) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return codec_type in result.stdout


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_raises_when_no_video_track(tmp_path):
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=2.0)
    timeline = TimelineState(
        duration_seconds=2.0,
        tracks=[Track(id="audio_original", type="audio_original")],
    )

    with pytest.raises(ValueError, match="video track"):
        render_export(timeline, source, {}, str(tmp_path / "out.mp4"))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_simple_trim_produces_correct_duration(tmp_path):
    """Source video is 4s, trimmed to keep exactly segment [1.0, 3.0] (= 2s),
    with no audio track at all — the output must be ~2s long (matching the
    declared duration_seconds), with both a video and an audio stream (silent
    audio auto-generated by _build_mixed_audio when there's no audible audio
    track)."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=4.0)
    timeline = TimelineState(
        duration_seconds=2.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=1.0, source_end=3.0)],
            )
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    result = render_export(timeline, source, {}, output_path)

    assert result == output_path
    assert _probe_duration(output_path) == pytest.approx(2.0, abs=0.15)
    assert _has_stream(output_path, "video")
    assert _has_stream(output_path, "audio")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_fills_gap_between_clips_with_black_filler(tmp_path):
    """2 video clips separated by a gap (a segment was deleted, NO ripple) —
    the total output duration must still match duration_seconds (including
    the filler segment)."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=5.0)
    timeline = TimelineState(
        duration_seconds=4.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[
                    Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=1.0),
                    # The gap [1.0, 3.0) has no clip -> a 2s filler must be
                    # inserted before reaching the 2nd clip.
                    Clip(id="v2", timeline_start=3.0, source_start=4.0, source_end=5.0),
                ],
            )
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {}, output_path)

    assert _probe_duration(output_path) == pytest.approx(4.0, abs=0.15)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_mixes_dialogue_and_audio_original_tracks(tmp_path):
    """A timeline with both an audio_original track (taken from the source
    video itself) and a dialogue track (taken from a separate dub file) —
    must not raise, and the output must have both video+audio streams and the
    correct duration_seconds."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=3.0)
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=0.5)

    line_id = uuid.uuid4()
    timeline = TimelineState(
        duration_seconds=3.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=3.0)],
            ),
            Track(
                id="audio_original",
                type="audio_original",
                volume=0.5,
                clips=[Clip(id="a1", timeline_start=0.0, source_start=0.0, source_end=3.0)],
            ),
            Track(
                id="character-x",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line_id,
                        timeline_start=1.0,
                        source_start=0.0,
                        source_end=0.5,
                        fade_in_seconds=0.1,
                        fade_out_seconds=0.1,
                    )
                ],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {line_id: dub_path}, output_path)

    assert _probe_duration(output_path) == pytest.approx(3.0, abs=0.15)
    assert _has_stream(output_path, "video")
    assert _has_stream(output_path, "audio")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_muted_track_still_produces_silent_output_not_crash(tmp_path):
    """Every audio track muted (only the video track remains) —
    _build_mixed_audio must auto-generate silence of the correct length,
    NOT let amix receive 0 inputs and error out."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=2.0)
    timeline = TimelineState(
        duration_seconds=2.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=2.0)],
            ),
            Track(
                id="audio_original",
                type="audio_original",
                muted=True,
                clips=[Clip(id="a1", timeline_start=0.0, source_start=0.0, source_end=2.0)],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {}, output_path)

    assert _probe_duration(output_path) == pytest.approx(2.0, abs=0.15)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_video_and_audio_streams_stay_in_sync_with_gap(tmp_path):
    """Regression test (found during review): previously, cursor accumulated
    from the DECLARED figures on Clip instead of re-measuring the REAL
    duration of each re-encoded segment — the video track could end a few
    frames earlier/later than the audio track SILENTLY (no crash).
    `_probe_duration` (which reads `format=duration`, usually reflecting the
    longest stream) doesn't catch this bug — the video-stream and
    audio-stream durations must be measured SEPARATELY and compared directly
    against each other."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=6.0)
    timeline = TimelineState(
        duration_seconds=4.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[
                    Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=1.0),
                    # Gap [1.0, 2.5) inserts a filler — the spot where the old
                    # bug was most likely to show up, since it accumulates
                    # across 2 consecutive re-encoded segments.
                    Clip(id="v2", timeline_start=2.5, source_start=3.0, source_end=4.5),
                ],
            ),
            Track(
                id="audio_original",
                type="audio_original",
                clips=[Clip(id="a1", timeline_start=0.0, source_start=0.0, source_end=4.0)],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {}, output_path)

    video_duration = _probe_stream_duration(output_path, "video")
    audio_duration = _probe_stream_duration(output_path, "audio")
    assert video_duration == pytest.approx(4.0, abs=0.1)
    assert audio_duration == pytest.approx(4.0, abs=0.1)
    # Comparing the 2 tracks directly against each other — this is the
    # measurement that actually catches an audio/video sync bug, not just a
    # mismatch against duration_seconds.
    # NOTE: the observed magnitude of fps-rounding drift in practice is very
    # small (~1 frame/segment, see the manual experiment during code review),
    # so this test may not reliably reproduce the OLD bug (cursor accumulated
    # from declared figures) using real data alone — see
    # test_build_silent_video_cursor_tracks_measured_duration_not_declared
    # below for a deterministic test of the actual fix mechanism.
    assert video_duration == pytest.approx(audio_duration, abs=0.1)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_build_silent_video_cursor_tracks_measured_duration_not_declared(tmp_path):
    """A deterministic test, independent of real ffmpeg's random rounding
    direction: confirms _build_silent_video uses _probe_duration (a REAL
    measurement) to accumulate cursor, not the declared
    `clip.source_end - clip.source_start` — by spying on it (wraps=the real
    function, no faked return value) and checking that the real function is
    ACTUALLY called exactly once per segment created. If it regresses to
    accumulating from the declared figures like the old code,
    _probe_duration would no longer be called in this loop — the spy would
    catch it immediately."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=5.0)
    timeline = TimelineState(
        duration_seconds=4.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[
                    Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=1.0),
                    Clip(id="v2", timeline_start=2.0, source_start=3.0, source_end=4.0),
                ],
            )
        ],
    )

    spy = patch(
        "worker.services.editor_export_service._probe_duration",
        wraps=editor_export_service._probe_duration,
    )
    with spy as mock_probe:
        _build_silent_video(timeline, source, str(tmp_path))

    # clip v1 [0,1) + filler in between (gap [1.0, 2.0)) + clip v2 (1.0s,
    # cursor -> 3.0) + trailing filler (gap [3.0, 4.0) since
    # duration_seconds=4.0) = 4 segments -> _probe_duration must be called
    # exactly 4 times to re-measure EACH real segment, NOT 0 times (the old
    # code's behavior, which only accumulated from declared figures).
    assert mock_probe.call_count == 4


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_leading_gap_before_first_clip(tmp_path):
    """The FIRST video clip doesn't start at timeline_start=0 (e.g. the
    opening segment was deleted without ripple enabled) — a leading filler
    must be inserted, not jump straight into the clip."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=5.0)
    timeline = TimelineState(
        duration_seconds=3.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=1.0, source_start=0.0, source_end=2.0)],
            )
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {}, output_path)

    assert _probe_duration(output_path) == pytest.approx(3.0, abs=0.15)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_mixes_multiple_dialogue_tracks_simultaneously(tmp_path):
    """2 dialogue tracks (2 different characters) at the same time, not just
    1 dialogue track + audio_original like the previous test — confirms amix
    correctly receives several simultaneous inputs, not just 2."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=3.0)
    dub1 = str(tmp_path / "dub1.wav")
    dub2 = str(tmp_path / "dub2.wav")
    _write_silent_wav(dub1, duration_seconds=0.5)
    _write_silent_wav(dub2, duration_seconds=0.5)
    line1_id, line2_id = uuid.uuid4(), uuid.uuid4()

    timeline = TimelineState(
        duration_seconds=3.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=3.0)],
            ),
            Track(
                id="character-a",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line1_id,
                        timeline_start=0.5,
                        source_start=0.0,
                        source_end=0.5,
                    )
                ],
            ),
            Track(
                id="character-b",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d2",
                        dialogue_line_id=line2_id,
                        timeline_start=2.0,
                        source_start=0.0,
                        source_end=0.5,
                    )
                ],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {line1_id: dub1, line2_id: dub2}, output_path)

    assert _probe_duration(output_path) == pytest.approx(3.0, abs=0.15)
    assert _has_stream(output_path, "audio")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_clip_volume_zero_does_not_crash(tmp_path):
    """volume=0 on a single clip (different from muting the whole track) —
    must still render normally, no filter error from gain=0."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=2.0)
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=0.5)
    line_id = uuid.uuid4()

    timeline = TimelineState(
        duration_seconds=2.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=2.0)],
            ),
            Track(
                id="character-x",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line_id,
                        timeline_start=0.0,
                        source_start=0.0,
                        source_end=0.5,
                        volume=0.0,
                    )
                ],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {line_id: dub_path}, output_path)

    assert _probe_duration(output_path) == pytest.approx(2.0, abs=0.15)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs real ffmpeg/ffprobe")
def test_render_export_fades_longer_than_half_clip_do_not_crash(tmp_path):
    """fade_in + fade_out both longer than half the clip (2 afades fully
    overlapping) — still syntactically valid as a filter, just needs to not
    crash."""
    source = str(tmp_path / "source.mp4")
    _make_test_video(source, duration=2.0)
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=1.0)
    line_id = uuid.uuid4()

    timeline = TimelineState(
        duration_seconds=2.0,
        tracks=[
            Track(
                id="video",
                type="video",
                clips=[Clip(id="v1", timeline_start=0.0, source_start=0.0, source_end=2.0)],
            ),
            Track(
                id="character-x",
                type="dialogue",
                character_id=uuid.uuid4(),
                clips=[
                    Clip(
                        id="d1",
                        dialogue_line_id=line_id,
                        timeline_start=0.0,
                        source_start=0.0,
                        source_end=1.0,
                        fade_in_seconds=0.8,
                        fade_out_seconds=0.8,
                    )
                ],
            ),
        ],
    )
    output_path = str(tmp_path / "out.mp4")

    render_export(timeline, source, {line_id: dub_path}, output_path)

    assert _probe_duration(output_path) == pytest.approx(2.0, abs=0.15)
