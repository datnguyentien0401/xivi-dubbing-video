from unittest.mock import AsyncMock

import pytest

from worker.agents.educational_agent import EducationalAgent
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
                translated_text="Halo teman-teman!",
            ),
        ],
    )


async def test_returns_vocabulary_and_quiz_from_gemini(context):
    mock_gemini = AsyncMock()
    mock_gemini.generate_educational_content.return_value = {
        "vocabulary": [{"term": "halo", "meaning": "xin chào", "example_sentence": "Halo!"}],
        "quiz": [{"question": "Halo nghĩa là gì?", "answer": "Xin chào"}],
    }

    agent = EducationalAgent(mock_gemini)
    result = await agent.run(context)

    assert result["vocabulary"][0]["term"] == "halo"
    assert len(result["quiz"]) == 1


async def test_does_not_fail_pipeline_when_gemini_errors(context):
    """Educational content is NOT on the critical path — an error here must
    not fail the whole job (see docs/agent-pipeline.md, worker/pipeline.py)."""
    mock_gemini = AsyncMock()
    mock_gemini.generate_educational_content.side_effect = RuntimeError("Gemini API error")

    agent = EducationalAgent(mock_gemini)
    result = await agent.run(context)

    assert result == {"vocabulary": [], "quiz": []}


async def test_returns_empty_when_no_translated_text_yet(context):
    context.dialogue_lines[0].translated_text = None
    mock_gemini = AsyncMock()

    agent = EducationalAgent(mock_gemini)
    result = await agent.run(context)

    mock_gemini.generate_educational_content.assert_not_awaited()
    assert result == {"vocabulary": [], "quiz": []}
