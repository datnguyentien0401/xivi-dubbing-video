from unittest.mock import AsyncMock

import pytest

from worker.agents.dubbing_agent import DubbingAgent
from worker.models.pipeline_context import Character, DialogueLine, PipelineContext


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
                id="line-ok",
                start_time=0.0,
                end_time=2.0,
                original_text="Xin chào!",
                translated_text="Halo!",
                safety_status="ok",
            ),
            DialogueLine(
                id="line-flagged",
                start_time=2.0,
                end_time=4.0,
                original_text="...",
                translated_text="...",
                safety_status="flagged",
            ),
            DialogueLine(
                id="line-already-dubbed",
                start_time=4.0,
                end_time=6.0,
                original_text="Tạm biệt!",
                translated_text="Sampai jumpa!",
                safety_status="ok",
                dubbed_audio_gcs_uri="gs://bucket/jobs/job-1/dubbed/line-already-dubbed.wav",
            ),
        ],
    )


async def test_only_dubs_lines_that_are_safety_ok_and_not_yet_dubbed(context):
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = b"fake-audio-bytes"
    mock_gcs = AsyncMock()
    mock_gcs.upload_bytes.return_value = "gs://bucket/jobs/job-1/dubbed/line-ok.wav"

    agent = DubbingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    mock_gemini.synthesize_dubbed_audio.assert_awaited_once()
    assert (
        result.dialogue_lines[0].dubbed_audio_gcs_uri == "gs://bucket/jobs/job-1/dubbed/line-ok.wav"
    )
    assert result.dialogue_lines[1].dubbed_audio_gcs_uri is None  # flagged — waiting for review
    assert result.current_stage == "dubbed"


async def test_redub_uses_a_new_blob_path_each_time(context):
    """Regression test for a real bug: redubbing a line wrote the new audio to
    the EXACT same old path (jobs/{job_id}/dubbed/{line_id}.wav) — the browser
    had already cached the response for that URL FROM BEFORE (e.g. before a
    prompt bug was fixed) and kept playing the OLD audio even though the
    backend had correctly overwritten it with the new audio, even after
    adding Cache-Control: no-store (the new header can't "un-poison" a
    browser cache entry saved BEFORE that header existed). Every dub must
    produce a NEW blob path — the same way a GCS signed URL always has its
    own expiring query param that self-busts the cache — so it doesn't depend
    on whether the browser honors the header or not."""
    context.dialogue_lines[2].dubbed_audio_gcs_uri = None  # allow redubbing both "ok" lines
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = b"fake-audio-bytes"
    mock_gcs = AsyncMock()
    mock_gcs.upload_bytes.side_effect = (
        lambda data, blob_name, content_type: f"gs://bucket/{blob_name}"
    )

    agent = DubbingAgent(mock_gemini, mock_gcs)
    await agent.run(context)

    blob_names = [call.args[1] for call in mock_gcs.upload_bytes.await_args_list]
    assert len(blob_names) == 2
    assert len(set(blob_names)) == 2  # 2 dubs (2 different lines) -> 2 different paths
    for name in blob_names:
        assert name.startswith("jobs/job-1/dubbed/")
        assert name.endswith(".wav")


async def test_prefers_voice_id_over_voice_name(context):
    """voice_id (Voice Design, matches the original voice characteristics
    better) MUST be preferentially passed to synthesize_dubbed_audio when a
    character has both a voice_id and a voice_name (e.g. an old character
    that was re-profiled) — see gemini_client.py::synthesize_dubbed_audio
    (voice_id is checked before voice_name)."""
    context.dialogue_lines = [context.dialogue_lines[0]]
    context.dialogue_lines[0].character_id = "char-1"
    context.characters = [
        Character(
            id="char-1",
            name="X",
            speaker_tag_original="speaker_1",
            voice_profile_metadata={"voice_id": "voice_abc123", "voice_name": "Kore"},
        )
    ]
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = b"fake-audio-bytes"
    mock_gcs = AsyncMock()
    mock_gcs.upload_bytes.return_value = "gs://bucket/jobs/job-1/dubbed/line-ok.wav"

    agent = DubbingAgent(mock_gemini, mock_gcs)
    await agent.run(context)

    call_kwargs = mock_gemini.synthesize_dubbed_audio.call_args.kwargs
    assert call_kwargs["voice_id"] == "voice_abc123"
    assert call_kwargs["voice_name"] == "Kore"  # still passed, but gemini_client prefers voice_id


async def test_passes_voice_name_selected_by_agent_2(context):
    """Regression test for the bug "the character's voice changed from female
    to male": DubbingAgent must use the voice_name that was already chosen by
    VoiceProfilingAgent by listening to a real audio sample (stored in
    character.voice_profile_metadata), NOT let GeminiClient fall back to
    hash(character_id) when a real choice already exists (see
    gemini_client.py:synthesize_dubbed_audio)."""
    context.dialogue_lines = [context.dialogue_lines[0]]
    context.dialogue_lines[0].character_id = "char-1"
    context.characters = [
        Character(
            id="char-1",
            name="X",
            speaker_tag_original="speaker_1",
            voice_profile_metadata={"voice_name": "Kore", "voice_selection_reasoning": "..."},
        )
    ]
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.return_value = b"fake-audio-bytes"
    mock_gcs = AsyncMock()
    mock_gcs.upload_bytes.return_value = "gs://bucket/jobs/job-1/dubbed/line-ok.wav"

    agent = DubbingAgent(mock_gemini, mock_gcs)
    await agent.run(context)

    call_kwargs = mock_gemini.synthesize_dubbed_audio.call_args.kwargs
    assert call_kwargs["voice_name"] == "Kore"


async def test_voice_style_falls_back_to_default_when_sentence_like():
    """Regression test for the real bug "the redubbed audio also contains the
    prompt": style_description is supposed to be just 2-4 short keywords
    (constrained in VOICE_SELECTION_PROMPT), but it's free text generated by
    Gemini — we can't be 100% sure it always complies. If a long sentence/one
    containing punctuation still slips into style_description, DubbingAgent
    MUST block it itself (defense-in-depth), not stuff it verbatim into
    DUBBING_AUDIO_INSTRUCTION for the TTS to read out loud."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri="gs://bucket/video.mp4",
        source_language="vi",
        target_language="id",
        characters=[
            Character(
                id="char-1",
                name="X",
                speaker_tag_original="speaker_1",
                voice_profile_metadata={
                    "style_description": (
                        "Chose this voice because it fits the character best, sounds warm."
                    )
                },
            ),
            Character(
                id="char-2",
                name="Y",
                speaker_tag_original="speaker_2",
                voice_profile_metadata={"style_description": "cheerful, gentle"},
            ),
        ],
    )
    agent = DubbingAgent(gemini=AsyncMock(), gcs=AsyncMock())

    assert agent._voice_style_for(context, "char-1") == "neutral, clear"
    # style_description is short, no signs of a full sentence -> kept as-is.
    assert agent._voice_style_for(context, "char-2") == "cheerful, gentle"


async def test_propagates_error_when_gemini_fails(context):
    context.dialogue_lines = [context.dialogue_lines[0]]
    mock_gemini = AsyncMock()
    mock_gemini.synthesize_dubbed_audio.side_effect = RuntimeError("Native Audio error")
    mock_gcs = AsyncMock()

    agent = DubbingAgent(mock_gemini, mock_gcs)
    with pytest.raises(RuntimeError):
        await agent.run(context)
