"""Tests for gemini_client.py — this file previously had no tests at all (see
review), so retry/cache-key/audio-config bugs weren't caught by CI.

Does not call Gemini for real (mocks `_client.models.generate_content`
directly, following the AGENTS.md convention "no need to call Gemini for
real in tests").
"""

import shutil
import subprocess
import wave
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import prompts
from worker.services.gemini_client import (
    GeminiClient,
    _pcm_to_wav,
    list_prebuilt_voices,
    voice_name_for_character,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class FakeRedis:
    """A simple double for RedisClient — an in-memory dict, no real Redis needed."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._store[key] = value


def _fake_response(audio_bytes: bytes = b"\x01\x02\x03\x04"):
    part = MagicMock()
    part.inline_data.data = audio_bytes
    response = MagicMock()
    response.candidates = [MagicMock(content=MagicMock(parts=[part]))]
    return response


def test_voice_name_for_character_is_stable_and_varies_by_character():
    a1 = voice_name_for_character("char-a")
    a2 = voice_name_for_character("char-a")
    b = voice_name_for_character("char-b")
    assert a1 == a2  # same character -> always the same voice
    # Doesn't assert a1 != b absolutely (the hash could coincidentally
    # collide given the small number of voices), but it must be a valid
    # voice name and the function must be deterministic.
    assert isinstance(b, str) and b


def test_voice_name_for_character_none_does_not_crash():
    assert isinstance(voice_name_for_character(None), str)


def test_list_prebuilt_voices_returns_all_12_with_descriptions():
    """specs/003-script-first-ingestion — GET /characters/voice-options reads
    this list directly for a human to pick a voice with no audio sample."""
    voices = list_prebuilt_voices()
    assert len(voices) == 12
    names = {v.voice_name for v in voices}
    assert len(names) == 12  # every voice_name is distinct, no duplicates
    assert all(v.description for v in voices)  # no blank description
    assert "Puck" in names  # one of the known prebuilt voices


def test_pcm_to_wav_produces_valid_wav_with_expected_params():
    pcm = b"\x00\x01" * 100  # 100 frames, 16-bit mono
    wav_bytes = _pcm_to_wav(pcm)
    with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.readframes(wav_file.getnframes()) == pcm


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_caches_and_wraps_pcm_as_wav():
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_response(b"\x01\x02"))

    result = await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
    )

    # The result must be valid WAV, not bare PCM.
    with wave.open(BytesIO(result), "rb") as wav_file:
        assert wav_file.getframerate() == 24000

    # Second call with the same key -> must come from cache, must NOT call Gemini again.
    client._client.models.generate_content.reset_mock()
    cached_result = await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
    )
    client._client.models.generate_content.assert_not_called()
    assert cached_result == result


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_cache_key_changes_when_prompt_content_changes():
    """Regression test for a real bug: after fixing DUBBING_AUDIO_INSTRUCTION
    to fix "dubbed audio contains the prompt itself", a dialogue line already
    dubbed before with the SAME (dialogue_line_id, target_lang, voice_name)
    still returned the OLD audio (generated from the old prompt) straight
    from the Redis cache — the code was fixed correctly but the stale cache
    masked it, and the user clicking "Redub" still heard the exact same bug.
    The cache key MUST change when the content actually sent to Gemini
    changes (simulated here by changing voice_style, since that's the only
    part of the instruction that can change between 2 calls with the same
    dialogue_line_id/target_lang/voice_name — the same consequence as
    changing the DUBBING_AUDIO_INSTRUCTION template itself)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    call_count = 0

    def _fake_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_response(bytes([call_count]))

    client._client.models.generate_content = MagicMock(side_effect=_fake_generate)

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
    )
    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="trầm, nghiêm nghị",  # same line/lang/voice, different prompt content
        character_id="char-1",
    )

    assert call_count == 2  # must NOT cache-hit between 2 different prompt contents


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_sends_minimal_instruction_without_meta_text():
    """Regression test for a real bug: "dubbed audio contains the prompt
    itself" — Gemini TTS (gemini-3.1-flash-tts-preview) treats the ENTIRE
    content sent to it as a transcript to read verbatim, unable to tell an
    instruction sentence apart from the text to read. The old version
    stuffed several long instruction clauses (target audience, language,
    target duration) before the dialogue, causing the model to sometimes
    read the instruction sentence aloud too. The `contents` sent now MUST
    contain ONLY `voice_style` + `text`, with no technical descriptor
    keywords mixed in (target language, number of seconds, "cartoon
    character")."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_response())

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="Xin chào các bạn!",
        target_lang="vi",
        voice_style="vui vẻ",
        character_id="char-1",
    )

    contents_sent = client._client.models.generate_content.call_args.kwargs["contents"]
    assert contents_sent == "Speak in a vui vẻ voice: Xin chào các bạn!\n"
    # Must not contain any of the old technical descriptor phrases (which
    # used to make the model misread them aloud).
    assert "seconds" not in contents_sent
    assert "cartoon" not in contents_sent


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_skips_style_instruction_when_voice_id_present():
    """Regression test for a real bug (reported 2026-10): EVERY character
    using Voice Design (voice_id) was assigned the exact same hardcoded
    `style_description` in voice_profiling_agent.py ("neutral, clear") —
    stuffing that value through DUBBING_AUDIO_INSTRUCTION caused the TTS
    model to read the sentence "Speak in a neutral, clear voice:" aloud at
    the START OF EVERY dialogue line. A Voice Design voice has already been
    matched to the original voice's characteristics — there's no need for
    (and it must not get) an extra style instruction sentence stuffed in;
    the `contents` sent to Gemini must be `text` VERBATIM, not routed
    through DUBBING_AUDIO_INSTRUCTION."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_response())

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="Xin chào các bạn!",
        target_lang="vi",
        voice_style="trung tính, rõ ràng",
        character_id="char-1",
        voice_id="voice_abc123",
    )

    contents_sent = client._client.models.generate_content.call_args.kwargs["contents"]
    assert contents_sent == "Xin chào các bạn!"
    assert "giọng" not in contents_sent


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_uses_character_specific_voice():
    seen_voice_names: list[str] = []

    def fake_generate_content(*, model, contents, config):  # noqa: ARG001
        seen_voice_names.append(config.speech_config.voice_config.prebuilt_voice_config.voice_name)
        return _fake_response()

    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(side_effect=fake_generate_content)

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-a",
        text="a",
        target_lang="id",
        voice_style="x",
        character_id="char-A",
    )
    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-b",
        text="b",
        target_lang="id",
        voice_style="x",
        character_id="char-B",
    )

    # Previously a bug: every character got "Puck" — now it must follow character_id.
    assert seen_voice_names[0] == voice_name_for_character("char-A")
    assert seen_voice_names[1] == voice_name_for_character("char-B")


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_does_not_double_wrap_when_already_wav():
    """Regression test for the "video/audio crash" bug in
    ReviewDashboard.tsx: verified firsthand via a real request to the
    current TTS model — the response returns
    `inline_data.mime_type="audio/wav"` and the data already has a real
    RIFF/WAVE header (NOT bare raw PCM as the old code assumed). Wrapping it
    in another `_pcm_to_wav` layer produces a WAV nested inside a WAV —
    ffmpeg/ffprobe report no error (any raw "PCM" is structurally valid) but
    Chrome refuses to play it with AUDIO_RENDERER_ERROR (confirmed via
    chrome://media-internals). When the response is already real WAV, it
    must be used AS-IS, with no extra wrapping."""
    already_wav = _pcm_to_wav(b"\x00\x01" * 50)  # real WAV, with a RIFF header
    assert already_wav[:4] == b"RIFF"  # confirm the test fixture is really WAV

    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_response(already_wav))

    result = await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
    )

    assert result == already_wav  # used as-is, must NOT nest another WAV layer


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_prefers_explicit_voice_name_over_hash():
    """Regression test for the "character's voice changed from female to
    male" bug: if Agent 2 already chose a real voice_name by listening to the
    audio sample (see select_voice_for_speaker), it MUST be used, and must
    not fall back to a random hash(character_id)."""
    seen_voice_names: list[str] = []

    def fake_generate_content(*, model, contents, config):  # noqa: ARG001
        seen_voice_names.append(config.speech_config.voice_config.prebuilt_voice_config.voice_name)
        return _fake_response()

    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(side_effect=fake_generate_content)

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-a",
        text="a",
        target_lang="id",
        voice_style="x",
        character_id="char-A",
        voice_name="Kore",
    )

    assert seen_voice_names[0] == "Kore"
    assert seen_voice_names[0] != voice_name_for_character("char-A")


@pytest.mark.asyncio
async def test_select_voice_for_speaker_uploads_sample_and_returns_valid_voice(tmp_path):
    """Regression test for the "character's voice changed from female to
    male" bug: the previous version chose a voice via hash(character_id) —
    never listening to the original audio. Now it must upload the real audio
    sample to the Gemini File API and ask Gemini to pick the closest-matching
    voice (verified this approach firsthand against the official docs —
    Google doesn't publish a gender for each voice, so it's better to let
    Gemini listen and choose rather than hardcoding a guessed-gender
    table)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response('{"voice_name": "Kore", "reasoning": "Giọng nữ, ấm"}')
    )

    result = await client.select_voice_for_speaker(str(tmp_path / "sample.wav"))

    assert result == {"voice_name": "Kore", "reasoning": "Giọng nữ, ấm"}
    client._client.files.upload.assert_called_once_with(file=str(tmp_path / "sample.wav"))
    client._client.files.delete.assert_called_once_with(name=uploaded_file.name)


@pytest.mark.asyncio
async def test_select_voice_for_speaker_falls_back_when_gemini_hallucinates_voice_name(tmp_path):
    """Gemini returns a voice name that is NOT in the real list (a
    hallucination) — must return voice_name=None so the caller (DubbingAgent
    via synthesize_dubbed_audio) falls back to the hash itself, and must not
    use the invalid name directly (which would error when calling Gemini
    Native Audio for real with a voice_name that doesn't exist)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response('{"voice_name": "NotARealVoice", "reasoning": "..."}')
    )

    result = await client.select_voice_for_speaker(str(tmp_path / "sample.wav"))

    assert result["voice_name"] is None


@pytest.mark.asyncio
async def test_describe_voice_from_sample_uploads_and_returns_description(tmp_path):
    """Step 1/2 of Voice Design (see design_voice) — listens to the real
    sample then writes a detailed description of the voice's characteristics
    in English, used as input for the voice-synthesis step (see
    voice_profiling_agent.py::build_voice_profile_metadata)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    description = "A warm, youthful female voice with a cheerful, playful tone."
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response(f'{{"description": "{description}"}}')
    )

    result = await client.describe_voice_from_sample(str(tmp_path / "sample.wav"))

    assert result == description
    client._client.files.upload.assert_called_once_with(file=str(tmp_path / "sample.wav"))
    client._client.files.delete.assert_called_once_with(name=uploaded_file.name)


@pytest.mark.asyncio
async def test_describe_voice_from_sample_propagates_error_for_caller_fallback(tmp_path):
    """Unlike select_voice_for_speaker (which falls back internally),
    describe_voice_from_sample MUST raise on error — the orchestration
    fallback (Voice Design -> prebuilt -> hash) lives in
    build_voice_profile_metadata, not inside GeminiClient."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.files.upload = MagicMock(side_effect=RuntimeError("upload failed"))

    with pytest.raises(RuntimeError):
        await client.describe_voice_from_sample(str(tmp_path / "sample.wav"))


@pytest.mark.asyncio
async def test_design_voice_calls_voices_create_with_prompted_type_and_returns_id():
    """Voice Design (client.voices.create, type="prompted") — synthesizes a
    NEW voice from a text description, requiring NO consent_audio (unlike
    Voice Replication). The request shape was verified by directly reading
    the real source/docstring of the google-genai 2.28.0 SDK
    (VoiceInput/PromptedVoice), not guessed."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    fake_voice_output = MagicMock()
    fake_voice_output.id = "voice_abc123"
    client._client.voices.create = MagicMock(return_value=fake_voice_output)

    result = await client.design_voice("A deep, booming male voice.", display_name="Gấu Xám")

    assert result == "voice_abc123"
    call_kwargs = client._client.voices.create.call_args.kwargs
    assert call_kwargs["voice"]["type"] == "prompted"
    assert call_kwargs["voice"]["prompted"]["input"] == "A deep, booming male voice."
    assert call_kwargs["voice"]["display_name"] == "Gấu Xám"
    assert call_kwargs["store"] is True


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_uses_voice_id_when_present():
    """voice_id (Voice Design) MUST be passed through VoiceConfig's `voice`
    field (NOT prebuilt_voice_config) — verified via the SDK's real
    docstring: VoiceConfig.voice = "The speaker identifier for synthesis."
    and VoiceOutput.id = "Pass {id} directly to SpeechConfig.voice_config.voice
    ... during speech synthesis.".

    Regression test for a real finding via a direct controlled test against
    the API: voice_id ONLY works with _VOICE_DESIGN_MODEL
    (gemini-3.8-flash-tts) — calling the same voice_id with _AUDIO_MODEL
    (gemini-3.1-flash-tts-preview, used for the prebuilt_voice_config branch)
    returns 500 INTERNAL 100% of attempts, even though the SDK docstring says
    voice_id works with "any model". The right model MUST be chosen based on
    whether voice_id is present, never defaulting to _AUDIO_MODEL."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    seen_configs = []
    seen_models = []

    def fake_generate_content(*, model, contents, config):  # noqa: ARG001
        seen_configs.append(config.speech_config.voice_config)
        seen_models.append(model)
        return _fake_response()

    client._client.models.generate_content = MagicMock(side_effect=fake_generate_content)

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
        voice_name="Kore",  # still passed but MUST be ignored when voice_id is present
        voice_id="voice_abc123",
    )

    assert seen_configs[0].voice == "voice_abc123"
    assert seen_configs[0].prebuilt_voice_config is None
    assert seen_models[0] == "gemini-3.8-flash-tts"


@pytest.mark.asyncio
async def test_synthesize_dubbed_audio_uses_audio_model_for_prebuilt_voice():
    """The prebuilt branch (no voice_id) must keep using the old
    _AUDIO_MODEL (gemini-3.1-flash-tts-preview, not subject to the 100/day
    quota) — only the voice_id branch needs to switch to
    _VOICE_DESIGN_MODEL (see the test above)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    seen_models = []

    def fake_generate_content(*, model, contents, config):  # noqa: ARG001
        seen_models.append(model)
        return _fake_response()

    client._client.models.generate_content = MagicMock(side_effect=fake_generate_content)

    await client.synthesize_dubbed_audio(
        dialogue_line_id="line-1",
        text="xin chao",
        target_lang="id",
        voice_style="vui vẻ",
        character_id="char-1",
        voice_name="Kore",
    )

    assert seen_models[0] == "gemini-3.1-flash-tts-preview"


@pytest.mark.asyncio
async def test_call_text_with_retry_retries_then_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # skip the real backoff so the test runs fast
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(side_effect=RuntimeError("transient"))

    with pytest.raises(RuntimeError, match="failed after 3 retries"):
        await client._call_text_with_retry("prompt", retries=3)


@pytest.mark.asyncio
async def test_translate_dialogue_includes_word_and_char_count_in_prompt():
    """Regression test: the translation must know the source text's length
    in order to prioritize a phrasing of comparable length when read
    naturally — previously the translation prompt had none of this
    information, so the translation came out abnormally long/short compared
    to the original, dumping the entire job of "forcing it to fit the
    duration" onto the later TTS + ffmpeg time-stretch step, which sounded
    abnormally sped up with overlapping audio between lines (received this
    feedback firsthand during a real review on Cloud Run — the first version
    using duration_seconds still wasn't tight enough; now uses word/char
    counts that Gemini can actually count/verify instead of inferring
    speaking speed from a number of seconds)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("Xin chào các bạn!")
    )

    await client.translate_dialogue(
        text="Hello everyone!", source_lang="en", target_lang="vi", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "2 words" in prompt_used  # "Hello everyone!".split() == 2
    assert "15 characters" in prompt_used  # len("Hello everyone!") == 15


@pytest.mark.asyncio
async def test_translate_dialogue_uses_char_count_only_for_non_space_delimited_source():
    """Regression test (discovered via gemini-prompt-reviewer, not a purely
    theoretical concern — ja/zh/th are genuinely valid source_lang values in
    NewProject.tsx): a Japanese sentence with no spaces gives word_count=1
    (or a few stray numbers from punctuation) — this meaningless number must
    NOT be fed into the prompt; only the character count should be used when
    the source OR target is a language that isn't space-delimited."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_text_response("Hello!"))

    japanese_text = "こんにちは"  # no spaces, .split() would give 1 meaningless "word"
    await client.translate_dialogue(
        text=japanese_text, source_lang="ja", target_lang="en", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    # "not space-delimited" is boilerplate specific to the char-only branch —
    # must not fall into the word_count branch (which would contain the
    # phrase "WORD COUNT and CHARACTER COUNT").
    assert "not space-delimited" in prompt_used
    assert "WORD COUNT and CHARACTER COUNT" not in prompt_used
    assert f"{len(japanese_text)} characters" in prompt_used


@pytest.mark.asyncio
async def test_translate_dialogue_tightens_length_tolerance_to_ten_percent():
    """Regression test (review 2026-10-10, comparing against
    docs/manual-dubbing-process.docx): the manual dubbing process this tool
    replaces holds translators to a ±10% length tolerance, not ±20% — the
    old, looser ±20% wording left too much slack for the downstream atempo
    time-stretch step (mux_service.py) to absorb without sounding
    sped-up/slowed-down."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("Xin chào các bạn!")
    )

    await client.translate_dialogue(
        text="Hello everyone!", source_lang="en", target_lang="vi", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "±10%" in prompt_used
    assert "±20%" not in prompt_used


@pytest.mark.asyncio
async def test_translate_dialogue_warns_to_compress_for_a_known_longer_target_language():
    """Regression test for a real gap found comparing against the manual
    dubbing process doc: that process explicitly tells translators "Spanish/
    French/German naturally run 15-25% longer than English, so MUST
    compress" — our prompt previously had no equivalent per-language
    directional hint at all, relying only on a flat tolerance that the
    translation would just happen to miss or hit. Translating INTO a
    known-longer language (see _LENGTH_EXPANSION_FROM_ENGLISH) must now
    proactively instruct Gemini to compress."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("¡Hola a todos!")
    )

    await client.translate_dialogue(
        text="Hello everyone!", source_lang="en", target_lang="es", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "longer than english" in prompt_used.lower()
    assert "compress" in prompt_used.lower()


@pytest.mark.asyncio
async def test_translate_dialogue_warns_against_padding_for_a_known_shorter_target_language():
    """Mirror of the compression case: translating INTO a language known to
    run SHORTER than English (e.g. Chinese, dense in character count) must
    warn against artificially padding the line, not just silently allow a
    too-short translation."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(return_value=_fake_text_response("你好！"))

    await client.translate_dialogue(
        text="Hello everyone!", source_lang="en", target_lang="zh", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "shorter than english" in prompt_used.lower()
    assert "pad" in prompt_used.lower()


@pytest.mark.asyncio
async def test_translate_dialogue_omits_expansion_note_for_language_without_a_known_tendency():
    """A target language absent from _LENGTH_EXPANSION_FROM_ENGLISH (no
    rule-of-thumb available) must not get a fabricated directional hint —
    absence must mean "no opinion", not be silently coerced into either
    branch."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("مرحبا بالجميع!")
    )

    await client.translate_dialogue(
        text="Hello everyone!", source_lang="en", target_lang="ar", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "longer than english" not in prompt_used.lower()
    assert "shorter than english" not in prompt_used.lower()


@pytest.mark.asyncio
async def test_translate_dialogue_omits_expansion_note_for_non_english_source():
    """Regression test (found via gemini-prompt-reviewer, review 2026-10-10):
    _LENGTH_EXPANSION_FROM_ENGLISH is a vs-ENGLISH rule of thumb; a
    non-English source (e.g. vi, a real SOURCE_LANGUAGES option — see
    NewProject.tsx) must not get an English-relative directional hint even
    when the target language IS in the dict — "id typically comes out 15%
    longer than ENGLISH" says nothing about how it compares to a Vietnamese
    source."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("Halo semua!")
    )

    await client.translate_dialogue(
        text="Xin chào các bạn!", source_lang="vi", target_lang="id", is_song=False
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert "longer than english" not in prompt_used.lower()
    assert "compress" not in prompt_used.lower()


@pytest.mark.asyncio
async def test_translate_dialogue_includes_context_and_marks_target_line():
    """Regression test for the "splitting the sentence into pieces broke the
    meaning of the whole sentence" bug: the translation prompt must contain
    the context of adjacent lines (built by TranslationAgent, see
    translation_agent.py:_build_context) so Gemini understands the whole
    sentence's meaning correctly, instead of just translating an isolated
    fragment of a split sentence independently."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("chúng ta học đếm số")
    )

    await client.translate_dialogue(
        text="chúng ta học đếm số",
        source_lang="vi",
        target_lang="id",
        is_song=False,
        context="    Hôm nay\n>>> chúng ta học đếm số\n    từ một đến mười",
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert ">>> chúng ta học đếm số" in prompt_used
    assert "    Hôm nay" in prompt_used
    assert "    từ một đến mười" in prompt_used


@pytest.mark.asyncio
async def test_translate_dialogue_prompt_does_not_wrap_target_line_in_quotes():
    """Regression test discovered via gemini-prompt-reviewer: the old prompt
    wrapped the line to translate in double quotes (`"{text}"`) right before
    the "return only the translation" instruction — this could easily make
    Gemini mimic that format and return the result wrapped in quotes too
    (which would leak into translated_text, used directly for TTS)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response("Xin chào!")
    )

    await client.translate_dialogue(
        text="Hello!", source_lang="en", target_lang="vi", is_song=False, context=">>> Hello!"
    )

    prompt_used = client._client.models.generate_content.call_args.kwargs["contents"]
    assert '"Hello!"' not in prompt_used


@pytest.mark.asyncio
async def test_translate_dialogue_cache_key_differs_by_context():
    """The same `text`/lang/is_song but a different surrounding context can
    translate to a different meaning (e.g. a different pronoun in the
    preceding line) — these must NOT be cached together, otherwise a later
    translated line could accidentally pick up the translation from a
    different context."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    call_count = 0

    def _fake_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_text_response(f"response-{call_count}")

    client._client.models.generate_content = MagicMock(side_effect=_fake_generate)

    await client.translate_dialogue(
        text="Hello!", source_lang="en", target_lang="vi", is_song=False, context="ctx-a"
    )
    await client.translate_dialogue(
        text="Hello!", source_lang="en", target_lang="vi", is_song=False, context="ctx-b"
    )

    assert call_count == 2  # different context -> must NOT cache-hit


@pytest.mark.asyncio
async def test_translate_dialogue_cache_hits_across_calls_with_identical_text():
    """The cache key no longer depends on duration (this parameter was
    dropped from translate_dialogue — the prompt content now only depends on
    text/source_lang/target_lang/is_song) — calling with the same source
    sentence twice must cache-hit, without calling Gemini a second time
    (wasting quota needlessly if not cached correctly)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    call_count = 0

    def _fake_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_text_response(f"response-{call_count}")

    client._client.models.generate_content = MagicMock(side_effect=_fake_generate)

    await client.translate_dialogue(
        text="Hello!", source_lang="en", target_lang="vi", is_song=False
    )
    await client.translate_dialogue(
        text="Hello!", source_lang="en", target_lang="vi", is_song=False
    )

    assert call_count == 1  # the 2nd call must cache-hit


def _fake_file(state: str, name: str = "files/abc", uri: str = "https://files.test/abc"):
    f = MagicMock()
    f.state = state
    f.name = name
    f.uri = uri
    f.mime_type = "video/mp4"
    f.error = None
    return f


def _fake_text_response(text: str):
    response = MagicMock()
    response.text = text
    return response


@pytest.mark.asyncio
async def test_transcribe_audio_uploads_file_and_passes_it_to_generate_content(
    monkeypatch, tmp_path
):
    """Regression test for the "transcript looks like mock data" bug:
    previously transcribe_audio did NOT attach any real video/audio to the
    request at all, so Gemini had to guess the content (reproduced this
    firsthand while testing on the cloud, verified the fix with a real
    request via the File API before writing this code)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(
        return_value=_fake_text_response(
            '[{"speaker_tag": "speaker_1", "start_time": 0.0, '
            '"end_time": 1.0, "text": "xin chào"}]'
        )
    )
    local_path = str(tmp_path / "video.mp4")

    result = await client.transcribe_audio(local_path)

    assert result == [
        {"speaker_tag": "speaker_1", "start_time": 0.0, "end_time": 1.0, "text": "xin chào"}
    ]
    client._client.files.upload.assert_called_once_with(file=local_path)
    # contents must be [Part(file), prompt] — not just plain prompt text
    # like the old bug.
    call_kwargs = client._client.models.generate_content.call_args.kwargs
    assert len(call_kwargs["contents"]) == 2
    # The uploaded file must be deleted after use (no need to keep it around needlessly).
    client._client.files.delete.assert_called_once_with(name=uploaded_file.name)


@pytest.mark.asyncio
async def test_transcribe_audio_waits_for_file_to_become_active(monkeypatch, tmp_path):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    processing_file = _fake_file(state="PROCESSING")
    active_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=processing_file)
    client._client.files.get = MagicMock(return_value=active_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(return_value=_fake_text_response("[]"))

    result = await client.transcribe_audio(str(tmp_path / "video.mp4"))

    assert result == []
    client._client.files.get.assert_called()


@pytest.mark.asyncio
async def test_transcribe_audio_passes_response_schema_to_enforce_numeric_timestamps(
    monkeypatch, tmp_path
):
    """Regression test for a real bug encountered (review 2026-10-09):
    Gemini returned start_time/end_time as a "mm:ss.mmm" string instead of a
    JSON number even though the prompt clearly asked for one, crashing the
    whole job. Instead of relying only on the wording in
    TRANSCRIPTION_PROMPT, transcribe_audio must pass a response_schema to
    enforce the type at the API layer (see GeminiClient._TranscriptionItem)."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    seen_configs = []

    def fake_generate_content(*, model, contents, config):  # noqa: ARG001
        seen_configs.append(config)
        return _fake_text_response("[]")

    client._client.models.generate_content = MagicMock(side_effect=fake_generate_content)

    await client.transcribe_audio(str(tmp_path / "video.mp4"))

    assert seen_configs[0].response_mime_type == "application/json"
    assert seen_configs[0].response_schema is not None


@pytest.mark.asyncio
async def test_transcribe_audio_omits_duration_constraint_when_probing_fails(monkeypatch, tmp_path):
    """local_path doesn't point to a real file here (same as every other
    transcribe_audio test in this file) — ffprobe can't read it, so probing
    must fail gracefully (logged, not raised) and the prompt sent to Gemini
    must be the plain TRANSCRIPTION_PROMPT with no duration constraint
    appended, rather than crashing the whole transcription over a
    prompt-quality nicety."""
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(return_value=_fake_text_response("[]"))

    await client.transcribe_audio(str(tmp_path / "video.mp4"))

    call_kwargs = client._client.models.generate_content.call_args.kwargs
    prompt_text = call_kwargs["contents"][1]
    assert prompt_text == prompts.TRANSCRIPTION_PROMPT


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _HAS_FFMPEG, reason="requires real ffmpeg/ffprobe (runs in the worker container)"
)
async def test_transcribe_audio_appends_duration_constraint_when_probing_succeeds(tmp_path):
    """Regression test for the real bug found via feedback: Gemini assigned a
    dialogue line an end_time of 102.5s for a video that was actually only
    65.8s long. When the local file's duration CAN be measured, the prompt
    sent to Gemini must include TRANSCRIPTION_DURATION_CONSTRAINT grounded
    with the real probed duration (see TRANSCRIPTION_DURATION_CONSTRAINT in
    shared/prompts.py and the deterministic code-level backstop in
    ingestion_agent.py::_drop_lines_past_video_duration)."""
    real_video_path = str(tmp_path / "video.mp4")
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
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    uploaded_file = _fake_file(state="ACTIVE")
    client._client.files.upload = MagicMock(return_value=uploaded_file)
    client._client.files.delete = MagicMock()
    client._client.models.generate_content = MagicMock(return_value=_fake_text_response("[]"))

    await client.transcribe_audio(real_video_path)

    call_kwargs = client._client.models.generate_content.call_args.kwargs
    prompt_text = call_kwargs["contents"][1]
    assert prompt_text.startswith(prompts.TRANSCRIPTION_PROMPT)
    assert prompts.TRANSCRIPTION_DURATION_CONSTRAINT.format(duration_seconds=4.0) in prompt_text


@pytest.mark.asyncio
async def test_transcribe_audio_raises_with_clear_error_when_file_stays_processing_too_long(
    monkeypatch, tmp_path
):
    """Regression test for a real bug encountered (2026-10-09): a video close
    to 3 minutes long stayed in PROCESSING even after exhausting retries, and
    the job was stuck forever with a vague error. This test locks in the
    desired BEHAVIOR (raise clearly once the wait truly times out) —
    _wait_until_file_active's retries=90 (180s) is not tested here since
    that would take too long/is an unnecessary simulation; uses a small
    retries value via monkeypatch for a fast test, confirming the timeout
    logic works correctly regardless of the specific value."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    processing_file = _fake_file(state="PROCESSING")
    client._client.files.get = MagicMock(return_value=processing_file)

    with pytest.raises(RuntimeError, match="did not become ACTIVE"):
        await client._wait_until_file_active(processing_file, retries=3, interval_seconds=0)

    assert client._client.files.get.call_count == 3


@pytest.mark.asyncio
async def test_transcribe_audio_raises_when_file_processing_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = GeminiClient(api_key="fake-key", redis=FakeRedis())
    failed_file = _fake_file(state="FAILED")
    client._client.files.upload = MagicMock(return_value=failed_file)
    generate_content_mock = MagicMock()
    client._client.models.generate_content = generate_content_mock

    with pytest.raises(RuntimeError, match="failed to process"):
        await client.transcribe_audio(str(tmp_path / "video.mp4"))

    # When the file fails, generateContent must not be called (there's no
    # valid content to transcribe).
    generate_content_mock.assert_not_called()
