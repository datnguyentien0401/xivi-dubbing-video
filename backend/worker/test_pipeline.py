"""Tests for pipeline.py — no dedicated test file existed for Pipeline before
specs/003-script-first-ingestion (it was only ever exercised indirectly via
worker/test_main.py). Added here specifically to cover the new runtime
stage-agent selection (ingestion vs. script_benchmark) and the conditional
"muxed" stage, both introduced by that feature.

Mocks every agent (AsyncMock) and monkeypatches mux_for_job — no real
Gemini/ffmpeg/GCS calls, per AGENTS.md's "no need to call Gemini for real in
tests"."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import worker.pipeline as pipeline_module
from worker.models.pipeline_context import PipelineContext
from worker.pipeline import Pipeline


def _context(*, source_video_gcs_uri: str | None, current_stage: str = "queued"):
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri=source_video_gcs_uri,
        source_language="en",
        target_language="id",
        current_stage=current_stage,
    )


def _agent_mock():
    """An agent double whose run() just returns the context unchanged —
    pipeline.py always overwrites current_stage itself right after calling
    agent.run(), so the mock doesn't need to set it."""
    mock = AsyncMock()
    mock.run.side_effect = lambda context: context
    return mock


@pytest.fixture
def agents():
    return {
        "ingestion": _agent_mock(),
        "script_benchmark": _agent_mock(),
        "voice_profiling": _agent_mock(),
        "translation": _agent_mock(),
        "safety": _agent_mock(),
        "dubbing": _agent_mock(),
        "educational": AsyncMock(run=AsyncMock(return_value={"vocabulary": [], "quiz": []})),
    }


@pytest.fixture
def pipeline(agents):
    return Pipeline(
        ingestion=agents["ingestion"],
        script_benchmark=agents["script_benchmark"],
        voice_profiling=agents["voice_profiling"],
        translation=agents["translation"],
        safety=agents["safety"],
        dubbing=agents["dubbing"],
        educational=agents["educational"],
        gcs=AsyncMock(),
    )


async def test_script_first_job_uses_script_benchmark_not_ingestion_and_skips_mux(
    pipeline, agents, monkeypatch
):
    """specs/003-script-first-ingestion — a job with no source video must run
    ScriptBenchmarkAgent for the "transcribed" slot (not IngestionAgent), and
    must never attempt to mux (there's no video yet)."""
    mux_for_job = AsyncMock()
    monkeypatch.setattr(pipeline_module, "mux_for_job", mux_for_job)
    context = _context(source_video_gcs_uri=None)

    result, _educational_result, _stage_durations = await pipeline.run(
        context, save_checkpoint=AsyncMock(), update_realtime_status=AsyncMock()
    )

    agents["script_benchmark"].run.assert_awaited_once()
    agents["ingestion"].run.assert_not_awaited()
    agents["voice_profiling"].run.assert_awaited_once()
    agents["translation"].run.assert_awaited_once()
    agents["safety"].run.assert_awaited_once()
    agents["dubbing"].run.assert_awaited_once()
    mux_for_job.assert_not_awaited()
    assert result.current_stage == "ready_for_review"


async def test_video_first_job_uses_ingestion_not_script_benchmark_and_runs_mux(
    pipeline, agents, monkeypatch
):
    mux_for_job = AsyncMock(return_value="gs://bucket/jobs/x/muxed/id.mp4")
    monkeypatch.setattr(pipeline_module, "mux_for_job", mux_for_job)
    context = _context(source_video_gcs_uri="gs://bucket/src.mp4")

    result, _educational_result, _stage_durations = await pipeline.run(
        context, save_checkpoint=AsyncMock(), update_realtime_status=AsyncMock()
    )

    agents["ingestion"].run.assert_awaited_once()
    agents["script_benchmark"].run.assert_not_awaited()
    mux_for_job.assert_awaited_once()
    assert result.muxed_video_gcs_uri == "gs://bucket/jobs/x/muxed/id.mp4"
    assert result.current_stage == "ready_for_review"


async def test_resuming_a_script_first_job_already_dubbed_skips_straight_to_ready(
    pipeline, agents, monkeypatch
):
    """A script-first job resumed at current_stage="dubbed" (the last real
    stage it has) must run NO agent at all — there is no "muxed" stage for
    it to fall into, unlike a video-first job at the same stage."""
    mux_for_job = AsyncMock()
    monkeypatch.setattr(pipeline_module, "mux_for_job", mux_for_job)
    context = _context(source_video_gcs_uri=None, current_stage="dubbed")

    result, _educational_result, _stage_durations = await pipeline.run(
        context, save_checkpoint=AsyncMock(), update_realtime_status=AsyncMock()
    )

    for agent in agents.values():
        if agent is agents["educational"]:
            continue
        agent.run.assert_not_awaited()
    mux_for_job.assert_not_awaited()
    assert result.current_stage == "ready_for_review"


async def test_resuming_a_video_first_job_already_dubbed_only_runs_mux(
    pipeline, agents, monkeypatch
):
    mux_for_job = AsyncMock(return_value="gs://bucket/jobs/x/muxed/id.mp4")
    monkeypatch.setattr(pipeline_module, "mux_for_job", mux_for_job)
    context = _context(source_video_gcs_uri="gs://bucket/src.mp4", current_stage="dubbed")

    result, _educational_result, _stage_durations = await pipeline.run(
        context, save_checkpoint=AsyncMock(), update_realtime_status=AsyncMock()
    )

    for agent in agents.values():
        if agent is agents["educational"]:
            continue
        agent.run.assert_not_awaited()
    mux_for_job.assert_awaited_once()
    assert result.current_stage == "ready_for_review"


async def test_run_returns_one_duration_per_stage_actually_run(
    pipeline, agents, monkeypatch
):
    """Performance-benchmarking support (per-agent latency, see
    docs/agent-pipeline.md): stage_durations must have exactly one entry per
    stage that actually ran this call — including "educational" — and must
    NOT include a stage that was skipped (e.g. "muxed" for a script-first
    job with no video, see test_script_first_job_... above)."""
    mux_for_job = AsyncMock()
    monkeypatch.setattr(pipeline_module, "mux_for_job", mux_for_job)
    # translation is made measurably slower than the rest, so a real
    # time.perf_counter() bug (e.g. measuring the wrong span, or always
    # returning 0) would show up as a wrong ordering, not just a missing key.
    slow_delay_seconds = 0.05

    async def slow_run(context):
        await asyncio.sleep(slow_delay_seconds)
        return context

    agents["translation"].run.side_effect = slow_run
    context = _context(source_video_gcs_uri=None)

    _result, _educational_result, stage_durations = await pipeline.run(
        context, save_checkpoint=AsyncMock(), update_realtime_status=AsyncMock()
    )

    assert set(stage_durations.keys()) == {
        "transcribed",
        "voice_profiled",
        "translated",
        "safety_checked",
        "dubbed",
        "educational",
    }
    assert all(duration >= 0 for duration in stage_durations.values())
    assert stage_durations["translated"] >= slow_delay_seconds
    assert stage_durations["translated"] > stage_durations["safety_checked"]
