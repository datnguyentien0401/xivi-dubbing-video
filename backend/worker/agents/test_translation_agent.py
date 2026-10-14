"""Sample test — template for writing tests for the remaining agents (see the
gemini-pipeline-agent skill, checklist item "Has a test with at least 1 valid
input case + 1 case where Gemini returns an error").
"""

from unittest.mock import AsyncMock

import pytest

from worker.agents.translation_agent import TranslationAgent
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
                id="line-1", start_time=0.0, end_time=2.0, original_text="Xin chào các bạn!"
            ),
            DialogueLine(
                id="line-2",
                start_time=2.0,
                end_time=6.0,
                original_text="Một hai ba, chúng ta cùng đếm nào!",
                is_song=True,
            ),
        ],
    )


async def test_translates_normal_dialogue_and_song_differently(context):
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "Halo teman-teman!"

    agent = TranslationAgent(mock_gemini)
    result = await agent.run(context)

    assert result.dialogue_lines[0].translated_text == "Halo teman-teman!"
    assert result.current_stage == "translated"

    # The song line must be called with is_song=True so the agent uses the
    # correct transcreation prompt
    calls = mock_gemini.translate_dialogue.call_args_list
    assert calls[1].kwargs["is_song"] is True


async def test_builds_context_from_neighbor_lines_for_normal_dialogue():
    """Regression test for the bug "a sentence split into pieces made the
    sentence's overall meaning wrong": dialogue_lines are split by timestamp
    so one sentence can be split into multiple lines — translating each line
    WITHOUT knowing the adjacent lines loses context, making it easy to
    mistranslate the whole sentence. The context must contain all 3 lines,
    correctly marking the line being translated with ">>>"."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(id="line-1", start_time=0.0, end_time=1.0, original_text="Hôm nay"),
            DialogueLine(
                id="line-2", start_time=1.0, end_time=2.0, original_text="chúng ta học đếm số"
            ),
            DialogueLine(
                id="line-3", start_time=2.0, end_time=3.0, original_text="từ một đến mười"
            ),
        ],
    )
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "đã dịch"

    agent = TranslationAgent(mock_gemini)
    await agent.run(context)

    middle_call_context = mock_gemini.translate_dialogue.call_args_list[1].kwargs["context"]
    assert middle_call_context == ("    Hôm nay\n>>> chúng ta học đếm số\n    từ một đến mười")


async def test_builds_context_at_list_boundaries_without_error():
    """_build_context uses max(0, ...)/min(len, ...) to clamp the window at
    both ends of the list (window=2 on each side) — the first line (no left
    neighbor) and the last line (no right neighbor) must not error out or
    incorrectly pick up context from the other side. Uses 5 lines so the
    window is actually clamped (a list of 3 lines or fewer would already be
    fully covered by window=2, so clamping couldn't be tested)."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(
                id=f"line-{i}", start_time=float(i), end_time=float(i + 1), original_text=t
            )
            for i, t in enumerate(["A", "B", "C", "D", "E"])
        ],
    )
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "đã dịch"

    agent = TranslationAgent(mock_gemini)
    await agent.run(context)

    calls = mock_gemini.translate_dialogue.call_args_list
    assert calls[0].kwargs["context"] == ">>> A\n    B\n    C"
    assert calls[4].kwargs["context"] == "    C\n    D\n>>> E"


async def test_builds_context_for_single_line_video():
    """A video with exactly 1 dialogue_line (no neighbors on either side) —
    the context must contain only that line itself, with no IndexError."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(id="line-1", start_time=0.0, end_time=1.0, original_text="Chỉ 1 dòng"),
        ],
    )
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "đã dịch"

    agent = TranslationAgent(mock_gemini)
    await agent.run(context)

    assert mock_gemini.translate_dialogue.call_args.kwargs["context"] == ">>> Chỉ 1 dòng"


async def test_marks_other_speakers_turn_in_context():
    """Regression test found via gemini-prompt-reviewer: an adjacent line in
    the context is NOT necessarily spoken by the same person as the line
    being translated (question/answer dialogue between 2 characters is
    normal) — without a clear annotation, Gemini might blend the meaning of
    two different characters' turns into one sentence, creating a NEW
    translation error."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        dialogue_lines=[
            DialogueLine(
                id="line-1",
                start_time=0.0,
                end_time=1.0,
                original_text="Bạn ăn chưa?",
                speaker_tag="speaker_1",
            ),
            DialogueLine(
                id="line-2",
                start_time=1.0,
                end_time=2.0,
                original_text="Rồi!",
                speaker_tag="speaker_2",
            ),
        ],
    )
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "đã dịch"

    agent = TranslationAgent(mock_gemini)
    await agent.run(context)

    first_call_context = mock_gemini.translate_dialogue.call_args_list[0].kwargs["context"]
    assert "[another speaker's turn" in first_call_context
    assert first_call_context == (
        ">>> Bạn ăn chưa?\n    Rồi!  [another speaker's turn — do NOT blend "
        "meaning with the >>> line]"
    )


async def test_does_not_build_context_for_song_lines():
    """Songs use transcreation as a whole from the original lyrics
    (SONG_TRANSCREATION_PROMPT) — they don't need/use adjacent-line context
    like normal dialogue does."""
    context = PipelineContext(
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
                original_text="Một hai ba",
                is_song=True,
            ),
        ],
    )
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.return_value = "đã dịch"

    agent = TranslationAgent(mock_gemini)
    await agent.run(context)

    assert mock_gemini.translate_dialogue.call_args.kwargs["context"] == ""


async def test_propagates_error_when_gemini_fails(context):
    mock_gemini = AsyncMock()
    mock_gemini.translate_dialogue.side_effect = RuntimeError("Gemini API error")

    agent = TranslationAgent(mock_gemini)
    with pytest.raises(RuntimeError):
        await agent.run(context)
