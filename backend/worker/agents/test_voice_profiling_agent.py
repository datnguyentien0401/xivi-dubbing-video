import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from worker.agents.voice_profiling_agent import VoiceProfilingAgent, _pick_sample_line
from worker.models.pipeline_context import Character, DialogueLine, PipelineContext

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


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
                speaker_tag="speaker_1",
                start_time=0.0,
                end_time=2.0,
                original_text="Xin chào!",
            ),
            DialogueLine(
                id="line-2",
                speaker_tag="speaker_1",
                start_time=2.0,
                end_time=4.0,
                original_text="Tôi khỏe!",
            ),
            DialogueLine(
                id="line-3",
                speaker_tag="speaker_2",
                start_time=4.0,
                end_time=6.0,
                original_text="Chào bạn!",
            ),
        ],
    )


def test_pick_sample_line_chooses_longest_line_for_tag():
    """A longer line gives a clearer signal for Gemini to detect the voice
    (gender/pitch/style) compared to a line that's too short."""
    lines = [
        DialogueLine(id="a", speaker_tag="s1", start_time=0.0, end_time=0.5, original_text="Ừ."),
        DialogueLine(
            id="b", speaker_tag="s1", start_time=1.0, end_time=4.0, original_text="Dài hơn nhiều."
        ),
        DialogueLine(
            id="c", speaker_tag="s2", start_time=5.0, end_time=9.0, original_text="Khác speaker."
        ),
    ]
    result = _pick_sample_line(lines, "s1")
    assert result.id == "b"


async def test_reuses_existing_character_for_known_speaker_tag(context):
    """speaker_1 must map back to the existing character (keeping the name
    "Bé Sóc" set by the creator in a previous episode), NOT create a
    duplicate character — this is the project's core value prop. Since every
    speaker_tag already has a character, it must NOT download the source
    video / call Gemini to pick a voice (wasted cost)."""
    context.characters = [
        Character(id="char-existing", name="Bé Sóc", speaker_tag_original="speaker_1"),
        Character(id="char-2", name="X", speaker_tag_original="speaker_2"),
    ]
    mock_gcs = AsyncMock()
    mock_gemini = AsyncMock()

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert len(result.characters) == 2
    assert result.dialogue_lines[0].character_id == "char-existing"
    assert result.dialogue_lines[1].character_id == "char-existing"
    mock_gcs.download_to_path.assert_not_called()
    mock_gemini.select_voice_for_speaker.assert_not_called()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
async def test_assigns_consistent_character_id_and_profiles_voice_from_real_audio(
    context, tmp_path
):
    """Regression test for the bug "the character's voice changed from
    female to male": the old version assigned a TTS voice via
    hash(character_id) — completely unrelated to the character's real voice
    in the source video. Now it must cut a real audio sample (that speaker's
    longest line) from the source video, hand it to Gemini to listen to and
    pick a matching voice, and save it to voice_profile_metadata for
    DubbingAgent to use instead of the hash (we personally received real
    feedback during a review on Cloud Run)."""
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
            "anullsrc=r=24000:cl=mono",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )

    async def fake_download(uri, local_path):
        shutil.copyfile(source_video, local_path)

    mock_gcs = AsyncMock()
    mock_gcs.download_to_path.side_effect = fake_download
    mock_gcs.upload_from_path.return_value = "gs://bucket/jobs/job-1/voice-samples/x.wav"

    mock_gemini = AsyncMock()
    # Voice Design (priority layer 1) is simulated to fail so it falls back
    # to select_voice_for_speaker (layer 2, the old approach) — matching this
    # test's original intent (there's a separate test for Voice Design
    # succeeding: test_prefers_voice_design_over_prebuilt_selection).
    mock_gemini.describe_voice_from_sample.side_effect = RuntimeError(
        "Voice Design lỗi (giả lập test)"
    )
    mock_gemini.select_voice_for_speaker.return_value = {
        "voice_name": "Kore",
        "style_description": "trầm, ấm",
        "reasoning": "Chọn giọng Kore vì mẫu giọng nghe trầm, phong cách ấm áp, phù hợp nhân vật.",
    }

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert len(result.characters) == 2
    assert result.dialogue_lines[0].character_id == result.dialogue_lines[1].character_id
    assert result.dialogue_lines[0].character_id != result.dialogue_lines[2].character_id
    assert result.current_stage == "voice_profiled"

    # Every NEW character must have its voice profiled from a real audio
    # sample, not assigned randomly.
    assert mock_gemini.select_voice_for_speaker.await_count == 2
    for character in result.characters:
        assert character.voice_profile_metadata["voice_name"] == "Kore"
        assert character.voice_sample_gcs_uri == "gs://bucket/jobs/job-1/voice-samples/x.wav"
        # Regression test: style_description (used by DubbingAgent via
        # _voice_style_for) used to never be set, every character always
        # fell back to the generic default string "neutral, clear" even
        # though Gemini had actually analyzed the real voice characteristics.
        assert character.voice_profile_metadata["style_description"] == "trầm, ấm"
        # Regression test for the bug "the redubbed audio also contains the
        # prompt": style_description MUST come from its own dedicated
        # style_description field, and MUST NOT be mistakenly taken from
        # `reasoning` (a long explanatory sentence) — stuffing a long
        # sentence into DUBBING_AUDIO_INSTRUCTION makes the TTS read that
        # whole sentence out loud.
        assert (
            character.voice_profile_metadata["style_description"]
            != (character.voice_profile_metadata["voice_selection_reasoning"])
        )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
async def test_prefers_voice_design_over_prebuilt_selection(context, tmp_path):
    """Voice Design (synthesizing a new voice matching the original voice's
    characteristics via a text description) is priority layer 1 — when it
    succeeds, select_voice_for_speaker (layer 2, picking from the 12 prebuilt
    voices) must NOT be called at all. The saved voice_id must be directly
    usable by DubbingAgent (see
    test_dubbing_agent.py::test_prefers_voice_id_over_voice_name)."""
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
            "anullsrc=r=24000:cl=mono",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )

    async def fake_download(uri, local_path):
        shutil.copyfile(source_video, local_path)

    mock_gcs = AsyncMock()
    mock_gcs.download_to_path.side_effect = fake_download
    mock_gcs.upload_from_path.return_value = "gs://bucket/sample.wav"

    mock_gemini = AsyncMock()
    mock_gemini.describe_voice_from_sample.return_value = (
        "A warm, youthful female voice with a cheerful, playful tone."
    )
    mock_gemini.design_voice.return_value = "voice_abc123"

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    assert len(result.characters) == 2
    mock_gemini.select_voice_for_speaker.assert_not_called()
    for character in result.characters:
        assert character.voice_profile_metadata["voice_id"] == "voice_abc123"
        assert "cheerful" in character.voice_profile_metadata["voice_description"]
        # Regression test for a real bug (reported 2026-10): the Voice
        # Design branch must NOT set "style_description" — the old version
        # hardcoded the identical "neutral, clear" for EVERY character
        # here, and then GeminiClient.synthesize_dubbed_audio stuffed that
        # value into the TTS instruction, causing the model to read that
        # sentence out loud at the start of EVERY dialogue line.
        assert "style_description" not in character.voice_profile_metadata
        # display_name passed to design_voice must be the real character
        # name, not the audio path or an empty value.
    design_voice_calls = mock_gemini.design_voice.call_args_list
    assert len(design_voice_calls) == 2
    display_names = {call.kwargs["display_name"] for call in design_voice_calls}
    assert display_names == {c.name for c in result.characters}


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
async def test_falls_back_to_default_style_when_gemini_omits_style_description(context, tmp_path):
    """If Gemini (or the hash fallback) doesn't return a style_description
    (e.g. only voice_name + reasoning, like a response missing a field
    because the model didn't follow the schema) — it MUST fall back to the
    safe default, and must NOT mistakenly use `reasoning` as the
    style_description (see the test above)."""
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
            "anullsrc=r=24000:cl=mono",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )

    async def fake_download(uri, local_path):
        shutil.copyfile(source_video, local_path)

    mock_gcs = AsyncMock()
    mock_gcs.download_to_path.side_effect = fake_download
    mock_gcs.upload_from_path.return_value = "gs://bucket/sample.wav"

    mock_gemini = AsyncMock()
    mock_gemini.describe_voice_from_sample.side_effect = RuntimeError(
        "Voice Design lỗi (giả lập test)"
    )
    mock_gemini.select_voice_for_speaker.return_value = {
        "voice_name": "Kore",
        "reasoning": "Chọn giọng Kore vì mẫu giọng nghe trầm, phong cách ấm áp, phù hợp nhân vật.",
    }

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    for character in result.characters:
        assert character.voice_profile_metadata["style_description"] == "neutral, clear"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="requires real ffmpeg (runs in the worker container)")
async def test_falls_back_to_none_voice_name_when_selection_fails(context, tmp_path):
    """A failure at BOTH voice-selection steps (Voice Design and
    select_voice_for_speaker, e.g. Gemini quota/network issues) must NOT fail
    the whole job — DubbingAgent falls back to hash(character_id) on its own
    when voice_id=None AND voice_name=None (see
    gemini_client.py:synthesize_dubbed_audio)."""
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
            "anullsrc=r=24000:cl=mono",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            source_video,
        ],
        check=True,
        capture_output=True,
    )

    async def fake_download(uri, local_path):
        shutil.copyfile(source_video, local_path)

    mock_gcs = AsyncMock()
    mock_gcs.download_to_path.side_effect = fake_download
    mock_gcs.upload_from_path.return_value = "gs://bucket/sample.wav"

    mock_gemini = AsyncMock()
    mock_gemini.describe_voice_from_sample.side_effect = RuntimeError(
        "Voice Design lỗi (giả lập test)"
    )
    mock_gemini.select_voice_for_speaker.side_effect = RuntimeError("Gemini lỗi")

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)  # does not raise

    assert result.characters[0].voice_profile_metadata.get("voice_id") is None
    assert result.characters[0].voice_profile_metadata["voice_name"] is None


async def test_is_a_safe_noop_for_a_script_first_job_with_no_source_video():
    """specs/003-script-first-ingestion — a script-first job's dialogue lines
    come from create_script_jobs with character_id ALREADY set (manual voice
    assignment, see api/routers/projects.py) and speaker_tag always None (no
    transcription ever happened). This agent must never try to download
    context.source_video_gcs_uri (None for such a job — would otherwise
    crash) specifically BECAUSE speaker_tags ends up empty when no line has a
    speaker_tag, so the `if new_tags:` download branch is never entered — no
    code change was needed in this agent for specs/003-script-first-ingestion
    to work correctly; this test exists to make that non-obvious invariant
    explicit and catch a future regression if it's ever broken."""
    context = PipelineContext(
        job_id="job-1",
        project_id="proj-1",
        source_video_gcs_uri=None,
        source_language="en",
        target_language="id",
        characters=[
            Character(id="char-1", name="Cat", speaker_tag_original="manual_abc"),
        ],
        dialogue_lines=[
            DialogueLine(
                id="line-1",
                character_id="char-1",
                speaker_tag=None,
                start_time=0.0,
                end_time=1.0,
                original_text="Hello!",
            ),
        ],
    )
    mock_gemini = AsyncMock()
    mock_gcs = AsyncMock()

    agent = VoiceProfilingAgent(mock_gemini, mock_gcs)
    result = await agent.run(context)

    mock_gcs.download_to_path.assert_not_awaited()
    mock_gemini.describe_voice_from_sample.assert_not_awaited()
    mock_gemini.select_voice_for_speaker.assert_not_awaited()
    assert result.dialogue_lines[0].character_id == "char-1"  # untouched
    assert result.current_stage == "voice_profiled"
