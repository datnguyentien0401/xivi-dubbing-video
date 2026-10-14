import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from worker.agents.ingestion_agent import (
    IngestionAgent,
    _drop_lines_past_video_duration,
    _parse_timestamp_seconds,
)
from worker.models.pipeline_context import PipelineContext

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture
def context() -> PipelineContext:
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
    )


async def test_builds_dialogue_lines_from_gemini_transcript(context):
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 2.0, "text": "Xin chào!"},
        {"speaker_tag": "speaker_2", "start_time": 2.0, "end_time": 4.0, "text": "Chào bạn!"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert len(result.dialogue_lines) == 2
    assert result.dialogue_lines[0].speaker_tag == "speaker_1"
    assert result.dialogue_lines[0].original_text == "Xin chào!"
    assert result.current_stage == "transcribed"
    # id must be a valid uuid, unique per line (used as PK for checkpointing)
    assert result.dialogue_lines[0].id != result.dialogue_lines[1].id
    # Must download the real video locally before calling transcribe_audio
    # (see worker/services/gemini_client.py — the Gemini Developer API needs
    # a local file path to upload via the File API, it can't read a gs://
    # URI directly).
    mock_gcs.download_to_path.assert_awaited_once()
    downloaded_uri = mock_gcs.download_to_path.await_args.args[0]
    assert downloaded_uri == context.source_video_gcs_uri
    local_path_arg = mock_gemini.transcribe_audio.await_args.args[0]
    # transcribe_audio must receive the downloaded local path (not the
    # gcs_uri — regression of the bug "transcript looks like mock data"
    # because transcribe_audio used to never receive any real video/audio
    # content at all).
    assert local_path_arg != context.source_video_gcs_uri
    assert local_path_arg.endswith(".mp4")  # keeps the original extension


async def test_normalizes_speaker_tag_across_short_pause_within_one_sentence(context):
    """Regression test for the bug "different voice clone even though it's
    the same character in the original sentence": when a sentence gets split
    by transcription into multiple lines due to a very short pause (e.g.
    0.1s), Gemini sometimes switches the speaker_tag label right at that
    split point even though it's still one person speaking continuously —
    the second line MUST be corrected back to the previous line's
    speaker_tag so that VoiceProfilingAgent assigns a single character_id,
    keeping one consistent dub voice for the whole sentence."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 1.0, "text": "Hôm nay"},
        # Gap is only 0.1s — same sentence split mid-way, not an actual change of speaker.
        {
            "speaker_tag": "speaker_2",
            "start_time": 1.1,
            "end_time": 2.0,
            "text": "chúng ta học đếm số",
        },
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert result.dialogue_lines[0].speaker_tag == "speaker_1"
    assert result.dialogue_lines[1].speaker_tag == "speaker_1"  # corrected from speaker_2


async def test_normalizes_speaker_tag_across_chain_of_three_split_fragments(context):
    """Sequential left->right processing must CORRECTLY merge an entire chain
    of 3+ split fragments (not just 2 adjacent lines) into the same
    speaker_tag — the 3rd line is compared against the 2nd line's
    speaker_tag AFTER it has already been corrected, not the original tag
    from Gemini."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 1.0, "text": "Hôm"},
        {"speaker_tag": "speaker_2", "start_time": 1.1, "end_time": 2.0, "text": "nay"},
        {"speaker_tag": "speaker_3", "start_time": 2.1, "end_time": 3.0, "text": "đẹp quá!"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert [line.speaker_tag for line in result.dialogue_lines] == [
        "speaker_1",
        "speaker_1",
        "speaker_1",
    ]


async def test_keeps_different_speaker_tag_when_gap_is_not_short(context):
    """A sufficiently long pause (a genuine switch to a different speaker)
    must NOT have its speaker_tag merged — avoid incorrectly collapsing two
    different characters into one."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 1.0, "text": "Xin chào!"},
        {"speaker_tag": "speaker_2", "start_time": 2.0, "end_time": 3.0, "text": "Chào bạn!"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert result.dialogue_lines[0].speaker_tag == "speaker_1"
    assert result.dialogue_lines[1].speaker_tag == "speaker_2"


async def test_builds_dialogue_lines_when_gemini_returns_mmss_timestamp_strings(context):
    """Regression test for a real bug encountered (reviewed 2026-10-09):
    TRANSCRIPTION_PROMPT clearly asks for "a float number of seconds" but
    there's no response_schema enforcing types at the API layer (see
    gemini_client.py::_JSON_CONFIG, shared across many call sites with
    different shapes so no per-site schema is attached) — Gemini
    occasionally returns a "mm:ss.mmm" string instead of a plain float,
    making DialogueLine(start_time=...) raise a ValidationError, crashing the
    whole job (no retry/resume mechanism). The agent must normalize both
    formats itself, not just plain floats."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {
            "speaker_tag": "speaker_1",
            "start_time": "00:02.100",
            "end_time": "00:04.200",
            "text": "a",
        },
        {"speaker_tag": "speaker_2", "start_time": "01:05", "end_time": "01:30.5", "text": "b"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert result.dialogue_lines[0].start_time == pytest.approx(2.1)
    assert result.dialogue_lines[0].end_time == pytest.approx(4.2)
    assert result.dialogue_lines[1].start_time == pytest.approx(65.0)
    assert result.dialogue_lines[1].end_time == pytest.approx(90.5)


@pytest.mark.parametrize(
    "value,expected",
    [
        (2.1, 2.1),
        (0, 0.0),
        ("2.1", 2.1),
        ("00:02.100", 2.1),
        ("02:05", 125.0),
        ("01:02:03.5", 3723.5),
    ],
)
def test_parse_timestamp_seconds_handles_numbers_and_mmss_strings(value, expected):
    assert _parse_timestamp_seconds(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [-5, -5.0, "-5", "nan", "NaN", "inf", "-inf", "Infinity"])
def test_parse_timestamp_seconds_rejects_negative_and_non_finite_values(value):
    """Regression test: Python's float() itself ACCEPTS negative numbers and
    nan/inf/-inf (case-insensitive) without raising — without this explicit
    check, a malformed transcript line of this kind would slip through
    silently, producing a DialogueLine with a nonsensical timestamp instead
    of being caught + skipped."""
    with pytest.raises(ValueError, match="invalid timestamp"):
        _parse_timestamp_seconds(value)


async def test_raises_when_every_single_line_fails_to_parse(context):
    """Regression test: `except ValueError` when building a DialogueLine also
    catches pydantic.ValidationError (a subclass of ValueError) for ANY
    validation error, not just timestamp errors — if ALL lines fail (more
    likely a real code bug, e.g. a new required field missing), the job must
    NOT be allowed to silently continue with an empty dialogue_lines (the
    video reaching the reviewer with no dialogue lines left at all, with no
    one knowing why)."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": "???", "end_time": 1.0, "text": "a"},
        {"speaker_tag": "speaker_2", "start_time": "???", "end_time": 2.0, "text": "b"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    with pytest.raises(RuntimeError, match="all"):
        await agent.run(context)


async def test_skips_single_malformed_line_instead_of_crashing_whole_job(context, caplog):
    """The response_schema at the Gemini layer (see
    gemini_client.py::_TranscriptionItem) and _parse_timestamp_seconds have
    greatly reduced the chance of hitting a malformed line, but don't give an
    absolute 100% guarantee. Previously a single bad line (e.g. an
    unparseable timestamp, a missing field) would crash the WHOLE job — with
    no retry/resume mechanism, the job would get stuck in "queued" forever.
    Now it must skip just the bad line, keeping the remaining valid lines."""
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 1.0, "text": "a"},
        # Bad line: the timestamp can't be parsed in any known format.
        {"speaker_tag": "speaker_2", "start_time": "???", "end_time": 2.5, "text": "b"},
        {"speaker_tag": "speaker_3", "start_time": 2.5, "end_time": 3.5, "text": "c"},
    ]
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert [line.original_text for line in result.dialogue_lines] == ["a", "c"]
    assert "skipping" in caplog.text


async def test_propagates_error_when_gemini_fails(context):
    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.side_effect = RuntimeError("Gemini API error")
    mock_gcs = AsyncMock()

    agent = IngestionAgent(mock_gemini, mock_gcs)
    with pytest.raises(RuntimeError):
        await agent.run(context)


def test_drop_lines_past_video_duration_keeps_lines_entirely_within_duration():
    items = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 2.0, "text": "a"},
        {"speaker_tag": "speaker_2", "start_time": 2.0, "end_time": 4.0, "text": "b"},
    ]

    assert _drop_lines_past_video_duration(items, 65.8, "job-1") == items


def test_drop_lines_past_video_duration_drops_line_starting_past_the_real_duration(caplog):
    """Regression test for the real "12 phút" line found on job
    f67c424a-c988-416b-b0e4-a99cbe291cf4: start_time=103.0 on a video that
    was only 65.8s long — there is no sensible position left for this line at
    all, so it must be dropped (not clamped) rather than risk recreating the
    same dead-air bug with a nonsensical [65.8, 65.8] zero-length slot."""
    items = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 2.0, "text": "a"},
        {"speaker_tag": "speaker_2", "start_time": 103.0, "end_time": 105.9, "text": "b"},
    ]

    result = _drop_lines_past_video_duration(items, 65.8, "job-1")

    assert [item["text"] for item in result] == ["a"]
    assert "dropping" in caplog.text


def test_drop_lines_past_video_duration_clamps_end_time_when_only_end_exceeds_duration(caplog):
    """Regression test for the real "ca đêm" line found on the same affected
    job: start_time=58.0 (still a valid position within the real 65.8s
    video), but end_time=102.5 (hallucinated far past the real end) — this
    line's speech genuinely started within the real video, so it must be
    KEPT with end_time clamped down to the real duration, not dropped
    outright (dropping it would lose that line's dub content for no
    reason)."""
    items = [
        {"speaker_tag": "speaker_1", "start_time": 58.0, "end_time": 102.5, "text": "ca đêm"},
    ]

    result = _drop_lines_past_video_duration(items, 65.8, "job-1")

    assert len(result) == 1
    assert result[0]["end_time"] == 65.8
    assert result[0]["start_time"] == 58.0  # untouched
    assert "clamping" in caplog.text


def test_drop_lines_past_video_duration_leaves_malformed_timestamps_for_the_existing_loop():
    """A line with an unparseable timestamp isn't this function's concern —
    it must pass through unchanged so IngestionAgent.run's existing
    try/except (which already handles malformed lines, see
    test_skips_single_malformed_line_instead_of_crashing_whole_job) is the
    one that catches and logs it, avoiding two different warning messages
    for what is really the same underlying problem."""
    items = [
        {"speaker_tag": "speaker_1", "start_time": "???", "end_time": 2.0, "text": "a"},
    ]

    assert _drop_lines_past_video_duration(items, 65.8, "job-1") == items


@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
async def test_run_clamps_and_drops_out_of_bounds_lines_against_the_real_probed_duration(
    context, tmp_path, caplog
):
    """End-to-end wiring test: IngestionAgent.run must probe the REAL
    downloaded video file's duration (not trust the nominal transcript
    timestamps) and apply _drop_lines_past_video_duration to the raw lines
    before building DialogueLine objects."""
    real_video_path = str(tmp_path / "downloaded.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=4",
            "-c:v",
            "libx264",
            real_video_path,
        ],
        check=True,
        capture_output=True,
    )

    mock_gemini = AsyncMock()
    mock_gemini.transcribe_audio.return_value = [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 2.0, "text": "a"},
        # Hallucinated far past the real 4s video — must be dropped.
        {"speaker_tag": "speaker_2", "start_time": 10.0, "end_time": 12.0, "text": "b"},
        # Starts within bounds but ends past it — must be kept, clamped.
        {"speaker_tag": "speaker_3", "start_time": 3.0, "end_time": 8.0, "text": "c"},
    ]
    mock_gcs = AsyncMock()

    async def _fake_download(_gcs_uri: str, local_path: str) -> None:
        shutil.copyfile(real_video_path, local_path)

    mock_gcs.download_to_path.side_effect = _fake_download

    agent = IngestionAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert [line.original_text for line in result.dialogue_lines] == ["a", "c"]
    assert result.dialogue_lines[1].end_time == pytest.approx(4.0, abs=0.2)
    assert "dropping" in caplog.text
    assert "clamping" in caplog.text
