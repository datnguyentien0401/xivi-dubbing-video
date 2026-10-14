from unittest.mock import AsyncMock

import pytest

from worker.agents.safety_agent import SafetyAgent
from worker.models.pipeline_context import DialogueLine, PipelineContext


@pytest.fixture
def context() -> PipelineContext:
    return PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(
                id="line-1",
                start_time=0.0,
                end_time=2.0,
                original_text="Xin chào!",
                translated_text="Halo!",
            ),
        ],
    )


async def test_flags_line_using_rules_loaded_from_db(context):
    mock_gemini = AsyncMock()
    mock_gemini.check_cultural_safety.return_value = {
        "safety_status": "flagged",
        "reasoning": "Nhắc tới rượu bia, không phù hợp Halal.",
    }
    rules_loader = AsyncMock(return_value={"id": "Tránh hình ảnh liên quan đến rượu bia."})

    agent = SafetyAgent(mock_gemini, rules_loader=rules_loader)
    result = await agent.run(context)

    rules_loader.assert_awaited_once()
    mock_gemini.check_cultural_safety.assert_awaited_once_with(
        text="Halo!",
        target_market="id",
        market_rules="Tránh hình ảnh liên quan đến rượu bia.",
    )
    assert result.dialogue_lines[0].safety_status == "flagged"
    assert result.dialogue_lines[0].safety_reasoning == "Nhắc tới rượu bia, không phù hợp Halal."
    assert result.current_stage == "safety_checked"


async def test_skips_lines_without_translated_text(context):
    context.dialogue_lines[0].translated_text = None
    mock_gemini = AsyncMock()
    rules_loader = AsyncMock(return_value={})

    agent = SafetyAgent(mock_gemini, rules_loader=rules_loader)
    result = await agent.run(context)

    mock_gemini.check_cultural_safety.assert_not_awaited()
    assert result.dialogue_lines[0].safety_status == "pending"
