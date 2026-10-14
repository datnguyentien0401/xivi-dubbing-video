import math
import os
import re
import shutil
import subprocess
import wave
from unittest.mock import AsyncMock, patch

import ffmpeg
import pytest

from worker.models.pipeline_context import DialogueLine, PipelineContext
from worker.services.mux_service import (
    _ATEMPO_MAX_RATIO,
    _ATEMPO_MIN_RATIO,
    _decompose_atempo_factors,
    _encode_audio_track,
    _format_srt_timestamp,
    _has_audio_stream,
    _pad_video_to_duration,
    _probe_duration,
    _write_srt,
    mux_dubbed_audio,
    mux_for_job,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture
def context() -> PipelineContext:
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/source.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(
                id="line-1",
                start_time=0.0,
                end_time=2.0,
                original_text="Xin chào!",
                translated_text="Halo!",
                safety_status="ok",
                dubbed_audio_gcs_uri="gs://bucket/jobs/job-1/dubbed/line-1.wav",
            ),
            DialogueLine(
                id="line-flagged",
                start_time=2.0,
                end_time=4.0,
                original_text="...",
                translated_text="...",
                safety_status="flagged",
            ),
        ],
    )


async def test_mux_for_job_downloads_only_dubbed_lines_and_uploads_result(context, tmp_path):
    mock_gcs = AsyncMock()
    mock_gcs.upload_from_path.return_value = "gs://bucket/jobs/job-1/muxed/id.mp4"

    with patch("worker.services.mux_service.mux_dubbed_audio") as mock_mux:
        result = await mux_for_job(context, mock_gcs, str(tmp_path))

    # only downloads the original video + audio of dubbed lines (not flagged/undubbed lines)
    assert mock_gcs.download_to_path.await_count == 2
    mock_mux.assert_called_once()
    dubbed_clips_arg = mock_mux.call_args[0][1]
    assert len(dubbed_clips_arg) == 1
    assert dubbed_clips_arg[0][0] == 0.0  # line-1's start_time
    assert dubbed_clips_arg[0][1] == 2.0  # line-1's end_time (for time-stretching to fit the slot)
    # Subtitles burned into the video use ALL lines with translated_text,
    # including flagged/non-dubbed lines — losing audio doesn't mean losing
    # the subtitles too.
    subtitle_lines_arg = mock_mux.call_args[0][2]
    assert len(subtitle_lines_arg) == 2
    mock_gcs.upload_from_path.assert_awaited_once()
    assert result == "gs://bucket/jobs/job-1/muxed/id.mp4"


async def test_mux_for_job_uses_a_new_blob_path_each_time(context, tmp_path):
    """Regression test for the same bug family as dubbing_agent.py: re-muxing
    (e.g. after redubbing a line) writes the new video to the EXACT same old
    path (jobs/{job_id}/muxed/{lang}.mp4) — a browser that already cached
    that URL from a previous view keeps playing the OLD video even though the
    backend correctly muxed a new one, even with Cache-Control: no-store (the
    new header can't "unpoison" a cache the browser already saved
    BEFOREHAND). Every mux must produce a NEW blob path."""
    mock_gcs = AsyncMock()
    captured_blob_names: list[str] = []

    async def fake_upload(local_path, blob_name):  # noqa: ARG001
        captured_blob_names.append(blob_name)
        return f"gs://bucket/{blob_name}"

    mock_gcs.upload_from_path.side_effect = fake_upload

    with patch("worker.services.mux_service.mux_dubbed_audio"):
        await mux_for_job(context, mock_gcs, str(tmp_path / "run1"))
        await mux_for_job(context, mock_gcs, str(tmp_path / "run2"))

    assert len(captured_blob_names) == 2
    assert captured_blob_names[0] != captured_blob_names[1]
    for name in captured_blob_names:
        assert name.startswith("jobs/job-1/muxed/id")
        assert name.endswith(".mp4")


async def test_mux_for_job_raises_when_no_line_was_dubbed(context, tmp_path):
    context.dialogue_lines = [context.dialogue_lines[1]]  # only the flagged line remains
    mock_gcs = AsyncMock()

    with pytest.raises(RuntimeError):
        await mux_for_job(context, mock_gcs, str(tmp_path))


@pytest.mark.parametrize(
    "ratio",
    [1.0, 1.1, 0.9, _ATEMPO_MAX_RATIO, _ATEMPO_MIN_RATIO, 3.0, 0.3, 8.0, 0.05],
)
def test_decompose_atempo_factors_product_matches_clamped_ratio(ratio):
    """Regression test for the "audio sounds overlapping" bug: a dub clip
    longer than the original line's slot (end_time - start_time) must be
    time-stretched (atempo) to fit the slot before mixing — otherwise it
    spills into the next line's slot, with the two audio tracks overlapping
    (reproduced this firsthand during a real review).

    The overall ratio is narrowly clamped to [_ATEMPO_MIN_RATIO,
    _ATEMPO_MAX_RATIO] (not [0.25, 4.0] as in the first version) — the first
    version allowed strong speed-up/slow-down, which sounded abnormally
    sped up (real feedback during review). The test still keeps a few
    extreme ratios (8.0, 0.05) to make sure the algorithm that splits into
    multiple atempo applications (each only accepting [0.5, 2.0]) still
    works correctly even when the input ratio is far outside the clamp
    range."""
    factors = _decompose_atempo_factors(ratio)
    for factor in factors:
        assert 0.5 <= factor <= 2.0
    product = math.prod(factors)
    expected = max(_ATEMPO_MIN_RATIO, min(_ATEMPO_MAX_RATIO, ratio))
    assert product == pytest.approx(expected, rel=1e-6)


def test_format_srt_timestamp():
    assert _format_srt_timestamp(0.0) == "00:00:00,000"
    assert _format_srt_timestamp(65.5) == "00:01:05,500"
    assert _format_srt_timestamp(3661.001) == "01:01:01,001"


def test_write_srt_produces_valid_sequential_format(tmp_path):
    """Regression test for "the original subtitles need to be replaced with
    translated subtitles" — previously mux never generated/burned in any
    subtitles at all, so the resulting video still kept the original
    subtitles (if any) or had no subtitles at all."""
    path = str(tmp_path / "subs.srt")
    _write_srt([(0.0, 1.5, "Xin chào!"), (1.5, 3.0, "Chào bạn!")], path)

    content = open(path, encoding="utf-8").read()
    assert content == (
        "1\n00:00:00,000 --> 00:00:01,500\nXin chào!\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\nChào bạn!\n\n"
    )


def _write_silent_wav(path: str, duration_seconds: float, framerate: int = 24000) -> None:
    n_frames = int(duration_seconds * framerate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00\x00" * n_frames)


def _mean_volume_db(path: str, start: float, duration: float) -> float:
    """Measures the mean_volume (dB) of an audio segment via ffmpeg's
    volumedetect filter — used to assert "has sound" (close to 0dB) or
    "silent" (very negative, e.g. <-50dB) without manually parsing PCM."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            path,
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    assert match, f"mean_volume not found in ffmpeg stderr:\n{result.stderr}"
    return float(match.group(1))


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
def test_encode_audio_track_preserves_original_audio_outside_dialogue_slots(tmp_path):
    """Regression test for a real bug encountered (review feedback
    2026-10-10): the output audio track was PREVIOUSLY built ENTIRELY from
    the dub clips, never referencing the original audio at all — every sound
    effect/background music/ambient sound (anything that isn't a
    character's dialogue) was lost completely throughout the whole video,
    not just while someone was speaking.

    Builds a fake "original video" with a 440Hz tone running the full 4s
    (representing sound effects/background music), with only one dub
    dialogue line that is SILENT within the slot [1.0, 2.0] — the output
    audio must: keep the original tone intact BEFORE and AFTER the dialogue
    slot, but be silent EXACTLY within the dialogue slot (not letting the
    original voice play over the dubbed voice at the same time, see the
    known limitation in _duck_original_audio_during_dialogue)."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=1.0)

    output_audio_path = str(tmp_path / "audio.m4a")
    _encode_audio_track(source_video, [(1.0, 2.0, dub_path)], output_audio_path)

    before = _mean_volume_db(output_audio_path, start=0.1, duration=0.7)
    during = _mean_volume_db(output_audio_path, start=1.15, duration=0.7)
    after = _mean_volume_db(output_audio_path, start=2.15, duration=1.7)

    assert (
        before > -50
    ), f"original audio BEFORE the dialogue slot was lost (mean_volume={before}dB)"
    assert after > -50, f"original audio AFTER the dialogue slot was lost (mean_volume={after}dB)"
    assert during < -50, (
        f"original audio PLAYED OVER the dubbed voice within the dialogue "
        f"slot (mean_volume={during}dB)"
    )


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
def test_encode_audio_track_resumes_original_audio_once_clamped_dub_clip_actually_ends(
    tmp_path,
):
    """Regression test for a real bug found via feedback: "the audio right
    before is stretched out even though it already ran out" — a ~40s silent
    gap appeared right before the video's last line. Root cause confirmed on
    the real job data: a dialogue line's transcribed slot was ~44.5s long but
    its actual dubbed clip was only ~4.6s — the ideal atempo ratio needed to
    stretch it to fill the slot (~0.1) is far past the intentionally narrow
    [_ATEMPO_MIN_RATIO, _ATEMPO_MAX_RATIO] clamp, so the clip only ever
    stretches to ~5.75s, nowhere near the full slot. The duck window used to
    always span the full NOMINAL slot regardless, muting the original
    audio/ambient sound for the entire mismeasured slot long after the dub
    clip had already finished — this test reproduces that exact shape
    (declared slot far longer than what the clip can be stretched to) and
    asserts the original audio resumes once the dub clip's REAL
    post-stretch duration ends, not at the nominal slot's declared end."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=0.5)

    output_audio_path = str(tmp_path / "audio.m4a")
    # Declared slot [1.0, 5.0] (4.0s) for a 0.5s clip -> ideal ratio 0.125,
    # clamped to _ATEMPO_MIN_RATIO=0.8 -> real post-stretch duration
    # 0.5/0.8=0.625s, ending at 1.625s — FAR before the slot's nominal end
    # at 5.0s.
    _encode_audio_track(source_video, [(1.0, 5.0, dub_path)], output_audio_path)

    before = _mean_volume_db(output_audio_path, start=0.1, duration=0.7)
    during_dub = _mean_volume_db(output_audio_path, start=1.1, duration=0.4)
    # Well after the dub clip's REAL end (1.625s) but still deep inside the
    # OLD nominal slot's range [1.0, 5.0] — the key regression check: the
    # old code kept this muted all the way to 5.0s.
    after_real_end_still_within_nominal_slot = _mean_volume_db(
        output_audio_path, start=3.0, duration=1.0
    )

    assert (
        before > -50
    ), f"original audio BEFORE the dialogue slot was lost (mean_volume={before}dB)"
    assert during_dub < -50, (
        f"original audio played over the dubbed clip while it was still "
        f"actually playing (mean_volume={during_dub}dB)"
    )
    assert after_real_end_still_within_nominal_slot > -50, (
        "original audio stayed muted long after the dub clip's real "
        f"post-stretch duration ended (mean_volume={after_real_end_still_within_nominal_slot}dB) "
        "— the duck window is still using the nominal slot instead of the "
        "actual stretched clip duration"
    )


def _write_tone_wav(
    path: str, duration_seconds: float, frequency: float = 440.0, framerate: int = 24000
) -> None:
    n_frames = int(duration_seconds * framerate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        amplitude = 16000
        samples = bytearray()
        for i in range(n_frames):
            value = int(amplitude * math.sin(2 * math.pi * frequency * i / framerate))
            samples += value.to_bytes(2, byteorder="little", signed=True)
        wf.writeframes(bytes(samples))


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
def test_encode_audio_track_skips_mixing_when_source_has_no_audio_stream(tmp_path):
    """The original video has no audio track at all (rare but possible) —
    _encode_audio_track must not crash, and the dubbed audio must genuinely
    be present in the output (not just asserting the file exists — must
    assert it actually has sound, to catch a bug that would cause the dub
    clip to also get lost along with it)."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-an",
            "-c:v",
            "libx264",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_tone_wav(dub_path, duration_seconds=0.5)
    output_audio_path = str(tmp_path / "audio.m4a")

    _encode_audio_track(source_video, [(0.0, 0.5, dub_path)], output_audio_path)

    assert os.path.exists(output_audio_path)
    during = _mean_volume_db(output_audio_path, start=0.05, duration=0.4)
    assert (
        during > -50
    ), f"dubbed audio was lost when the source has no audio track (mean_volume={during}dB)"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffprobe")
def test_has_audio_stream_detects_presence_and_absence(tmp_path):
    with_audio = str(tmp_path / "with_audio.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            with_audio,
        ],
        check=True,
        capture_output=True,
    )
    without_audio = str(tmp_path / "without_audio.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=1",
            "-an",
            "-c:v",
            "libx264",
            without_audio,
        ],
        check=True,
        capture_output=True,
    )
    assert _has_audio_stream(with_audio) is True
    assert _has_audio_stream(without_audio) is False


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
def test_mux_dubbed_audio_preserves_full_video_duration_even_with_short_dubbed_audio(tmp_path):
    """Regression test for the "original video is much longer than the
    resulting video" bug: previously `-shortest` was used on the ffmpeg
    output, trimming the WHOLE output to the SHORTER stream — the mixed
    audio track is usually much shorter than the video track (many stretches
    with nobody speaking). Reproduced this firsthand during a real review:
    original video ~15s, result only ~7s. This test builds a fake 4s video +
    just one very short dub audio clip (0.3s) placed in the first second —
    if the bug comes back, the output will only be a bit under 1s long
    instead of close to the full 4s."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )

    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=0.3)

    output_path = str(tmp_path / "output.mp4")
    mux_dubbed_audio(
        source_video_path=source_video,
        dubbed_audio_clips=[(0.5, 0.8, dub_path)],
        subtitle_lines=[(0.5, 0.8, "test")],
        output_path=output_path,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            output_path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output_duration = float(probe.stdout.strip())
    assert (
        output_duration > 3.5
    )  # close to the original 4s, must NOT be trimmed to the 0.3s dub audio


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
def test_encode_audio_track_pads_final_frame_instead_of_emitting_malformed_short_packet(tmp_path):
    """Regression test for AUDIO_RENDERER_ERROR (code 19) in Chrome: caught
    this firsthand via chrome://media-internals on a real video — the error
    occurs right when playback reaches NEAR THE END of the audio track (not
    when opening the stream), and via ffprobe the LAST audio packet is only
    6 bytes/0.000363s long (compared to ~350+ bytes/0.023220s for every
    other packet — 0.023220s = exactly one AAC frame of 1024 samples @
    44.1kHz). Root cause: the total number of samples after the whole
    atempo/adelay/amix chain rarely divides evenly by 1024, and the raw AAC
    encoder flushes the remainder as a malformed packet instead of
    zero-padding it to a full frame — Chrome's decoder (FFmpegAudioDecoder,
    with no hardware decoder for AAC) rejects this packet. Builds a 1.3s
    clip (1.3 / (1024/44100) ≈ 55.98 frames — GUARANTEED to have a
    remainder) to reproduce it, then asserts the last packet's duration must
    be ~equal to one full frame (asetnsamples pad=1 must handle it
    correctly)."""
    # The source video has NO audio track (-an) — keeps this test focused on
    # exactly one thing (the AAC encoder's final frame), unaffected by the
    # newly added original-audio handling (see
    # test_encode_audio_track_preserves_original_audio_outside_dialogue_slots).
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-an",
            "-c:v",
            "libx264",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=1.3, framerate=24000)

    output_audio_path = str(tmp_path / "audio.m4a")
    _encode_audio_track(source_video, [(0.0, 1.3, dub_path)], output_audio_path)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "packet=duration_time",
            "-of",
            "csv=p=0",
            output_audio_path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    packet_durations = [float(line.rstrip(",")) for line in probe.stdout.strip().splitlines()]
    last_frame_seconds = 1024 / 44100
    assert packet_durations[-1] > last_frame_seconds * 0.9, (
        f"last packet is only {packet_durations[-1]}s long — still a malformed "
        f"fragment, not a full frame of ~{last_frame_seconds}s"
    )


def _moov_atom_offset(path: str) -> int:
    """Returns the byte offset of the "moov" atom in an MP4 file — parses
    the MP4 box structure directly (ftyp/free/mdat/moov...), no external
    library needed."""
    with open(path, "rb") as f:
        data = f.read()
    pos = 0
    while pos < len(data) - 8:
        size = int.from_bytes(data[pos : pos + 4], "big")
        atom_type = data[pos + 4 : pos + 8].decode("ascii", errors="replace")
        if atom_type == "moov":
            return pos
        if size < 8:
            break
        pos += size
    raise AssertionError(f"'moov' atom not found in {path}")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
def test_mux_dubbed_audio_writes_faststart_moov_atom_near_start(tmp_path):
    """Regression test for the "video crash/won't play in ReviewDashboard"
    bug: by default ffmpeg writes the "moov" atom (metadata the browser
    needs to read first to know the file's structure/duration) at the END
    of the file — the file is still 100% valid per ffprobe (which only
    reads it after downloading the WHOLE file), but an HTML5 <video> playing
    over a URL (streaming/progressive, see ReviewDashboard.tsx) can't read
    it, and it looks "stuck"/crashed even though the file itself isn't
    broken — reproduced this firsthand by downloading a real video from GCS
    and manually parsing the MP4 box structure, confirming moov sits at the
    very last byte of the file. Needs movflags=faststart so ffmpeg writes
    moov at the START of the file (right after ftyp, before mdat)."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=0.3)
    output_path = str(tmp_path / "output.mp4")

    mux_dubbed_audio(
        source_video_path=source_video,
        dubbed_audio_clips=[(0.0, 0.3, dub_path)],
        subtitle_lines=[(0.0, 0.3, "test")],
        output_path=output_path,
    )

    file_size = os.path.getsize(output_path)
    moov_offset = _moov_atom_offset(output_path)
    # moov must sit NEAR THE START of the file (before the mdat part, which
    # takes up most of the size), not at the end — a generous 50% threshold
    # to avoid being fragile against ftyp/free overhead, while still
    # reliably catching the "moov at the end" bug (previously moov sat at
    # >99% of the file's offset).
    assert moov_offset < file_size * 0.5


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
def test_pad_video_to_duration_extends_with_frozen_last_frame(tmp_path):
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    output_path = str(tmp_path / "padded.mp4")

    _pad_video_to_duration(source_video, target_duration=5.0, output_path=output_path)

    assert _probe_duration(output_path) == pytest.approx(5.0, abs=0.2)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
def test_pad_video_to_duration_is_a_noop_when_already_long_enough(tmp_path):
    """target_duration SHORTER than the video's own real length (the normal
    case, since audio almost always ends up shorter than/equal to the
    source) must NOT trim/shorten the video — just pass it through as-is."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    output_path = str(tmp_path / "padded.mp4")

    _pad_video_to_duration(source_video, target_duration=1.0, output_path=output_path)

    assert _probe_duration(output_path) == pytest.approx(4.0, abs=0.2)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
def test_mux_dubbed_audio_pads_video_when_dialogue_timestamps_exceed_source_length(tmp_path):
    """Regression test for a real bug found via feedback: "the video has a
    ~40s pause right before the final line." Root-caused on real job data: a
    dialogue line's transcribed end_time (102.5s) and the NEXT line's
    start/end (103-105.9s) both landed past the SOURCE VIDEO's own real
    duration (65.8s, confirmed via ffprobe on the actual job) — the final
    muxed container ended up with a video track (65.8s) shorter than its
    audio track (106.2s), confirmed by probing each stream's duration
    separately. A container with mismatched track durations plays back
    unpredictably (most players stop at the shorter video stream or freeze
    on the last frame while audio keeps going underneath) — NOT an actual
    pause in the video content, a malformed container.

    Reproduces the same shape at a tiny scale: a 2s source video, with a dub
    clip positioned to start at 1.5s whose real content runs to ~2.8s —
    PAST the video's own 2s length. The final muxed file's video and audio
    streams must end up with CONSISTENT durations (video >= audio), not the
    video being shorter."""
    source_video = str(tmp_path / "source.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )
    dub_path = str(tmp_path / "dub.wav")
    _write_silent_wav(dub_path, duration_seconds=1.3)
    output_path = str(tmp_path / "output.mp4")

    # Declared slot [1.5, 1.6] (0.1s, the MIN_DIALOGUE_SLOT_SECONDS floor)
    # for a 1.3s clip -> ratio clamped to _ATEMPO_MIN_RATIO=0.8 -> real
    # post-stretch duration 1.3/0.8=1.625s, ending at 1.5+1.625=3.125s — PAST
    # the 2s source video.
    mux_dubbed_audio(
        source_video_path=source_video,
        dubbed_audio_clips=[(1.5, 1.6, dub_path)],
        subtitle_lines=[(1.5, 1.6, "test")],
        output_path=output_path,
    )

    probe = ffmpeg.probe(output_path)
    video_duration = float(
        next(s for s in probe["streams"] if s["codec_type"] == "video")["duration"]
    )
    audio_duration = float(
        next(s for s in probe["streams"] if s["codec_type"] == "audio")["duration"]
    )
    assert video_duration >= audio_duration - 0.2, (
        f"video track ({video_duration}s) is shorter than the audio track "
        f"({audio_duration}s) — the final line's audio would be orphaned "
        "past the end of the video, playing against a stopped/frozen player"
    )
