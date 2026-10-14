"""Tests for script_benchmark_agent.py — mocks GeminiClient.synthesize_dubbed_audio
directly (no need to call Gemini for real in tests, per AGENTS.md)."""

import wave
from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from worker.agents.script_benchmark_agent import ScriptBenchmarkAgent
from worker.models.pipeline_context import DialogueLine, PipelineContext


def _wav_bytes(duration_seconds: float, *, frame_rate: int = 24000) -> bytes:
    """A real, valid WAV file of exactly `duration_seconds` long (silence) —
    used so _wav_duration_seconds (reading the header back) has a known,
    verifiable value to assert against."""
    num_frames = int(duration_seconds * frame_rate)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(b"\x00\x00" * num_frames)
    return buffer.getvalue()


@pytest.fixture
def context() -> PipelineContext:
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri=None,
        source_language="en",
        target_language="id",
        dialogue_lines=[
            DialogueLine(
                id="line-1", start_time=0.0, end_time=1.0, original_text="Hello everyone!"
            ),
            DialogueLine(id="line-2", start_time=1.0, end_time=2.0, original_text="Hi there!"),
        ],
    )


async def test_lays_out_real_timestamps_cumulatively_from_measured_duration(context):
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.side_effect = [
        _wav_bytes(2.5),
        _wav_bytes(1.0),
    ]

    agent = ScriptBenchmarkAgent(mock_gemini)
    result = await agent.run(context)

    assert result.dialogue_lines[0].start_time == pytest.approx(0.0)
    assert result.dialogue_lines[0].end_time == pytest.approx(2.5, abs=0.01)
    # Second line starts exactly where the first one's REAL (measured, not
    # placeholder) duration ended — this is the whole point of the benchmark.
    assert result.dialogue_lines[1].start_time == pytest.approx(2.5, abs=0.01)
    assert result.dialogue_lines[1].end_time == pytest.approx(3.5, abs=0.01)
    assert result.current_stage == "transcribed"


async def test_calls_synthesize_dubbed_audio_with_english_and_no_character(context):
    """The benchmark is English regardless of target_language, and uses no
    specific character voice (character_id=None, voice_name=None,
    voice_id=None) — voice identity doesn't matter, this audio is measured
    for duration only and immediately discarded."""
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = _wav_bytes(1.0)

    agent = ScriptBenchmarkAgent(mock_gemini)
    await agent.run(context)

    first_call = mock_gemini.synthesize_dubbed_audio.await_args_list[0]
    assert first_call.kwargs["target_lang"] == "en"
    assert first_call.kwargs["character_id"] is None
    assert first_call.kwargs["voice_name"] is None
    assert first_call.kwargs["voice_id"] is None
    assert first_call.kwargs["text"] == "Hello everyone!"


async def test_dialogue_line_id_is_prefixed_so_it_cannot_cache_collide_with_a_real_dub(context):
    """Regression test (found via gemini-prompt-reviewer, review 2026-10-11):
    gemini_client.py::_cache_key does not include character_id, and uses the
    pre-resolution voice_name (None here) — without a distinct id prefix, a
    same-language, same-text, same-fallback-style real DubbingAgent call for
    this exact line could cache-hit this discarded benchmark audio and
    silently ship it as the final dub."""
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = _wav_bytes(1.0)

    agent = ScriptBenchmarkAgent(mock_gemini)
    await agent.run(context)

    first_call = mock_gemini.synthesize_dubbed_audio.await_args_list[0]
    assert first_call.kwargs["dialogue_line_id"] == "benchmark:line-1"


async def test_skips_single_line_whose_synthesis_fails_instead_of_crashing_whole_job(
    context, caplog
):
    """Regression test mirroring ingestion_agent.py's "skip 1 bad line"
    pattern — a single rare Gemini failure must not crash the whole job."""
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.side_effect = [
        RuntimeError("Gemini TTS quota exhausted"),
        _wav_bytes(1.0),
    ]

    agent = ScriptBenchmarkAgent(mock_gemini)
    result = await agent.run(context)

    assert len(result.dialogue_lines) == 1
    assert result.dialogue_lines[0].id == "line-2"
    # The surviving line still starts at 0.0 — the cursor is NOT advanced for
    # a line that failed and got excluded.
    assert result.dialogue_lines[0].start_time == pytest.approx(0.0)
    assert "skipping benchmark" in caplog.text


async def test_raises_when_every_single_line_fails(context):
    """Regression test mirroring ingestion_agent.py's identical guard: ALL
    lines failing is more likely a real outage/quota issue than normal bad
    data, and must surface immediately rather than silently producing a job
    with zero dialogue lines left to review."""
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.side_effect = RuntimeError("Gemini TTS quota exhausted")

    agent = ScriptBenchmarkAgent(mock_gemini)
    with pytest.raises(RuntimeError, match="all 2"):
        await agent.run(context)


async def test_noop_on_a_job_with_no_dialogue_lines():
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri=None,
        source_language="en",
        target_language="id",
        dialogue_lines=[],
    )
    mock_gemini = AsyncMock()

    agent = ScriptBenchmarkAgent(mock_gemini)
    result = await agent.run(context)

    assert result.dialogue_lines == []
    mock_gemini.synthesize_dubbed_audio.assert_not_awaited()
