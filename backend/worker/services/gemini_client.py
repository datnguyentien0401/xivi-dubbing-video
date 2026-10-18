"""The ONLY wrapper calling the Gemini API anywhere in this project.

Every agent must call through here (never import google.genai directly in an
agent) — to centralize retry/cache/rate-limit logic in one place (see
AGENTS.md principle #3 and docs/tech-stack.md section 3 on why Redis caching
is used).
"""

import asyncio
import base64
import hashlib
import json
import logging
import wave
from io import BytesIO

import ffmpeg
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel

from api.schemas.schemas import VoiceOption
from shared import prompts
from worker.services.redis_client import RedisClient

logger = logging.getLogger(__name__)

_TEXT_MODEL = "gemini-3.6-flash"  # verified directly against the real API (2026-09-28)
# — the old "gemini-2.5-flash" has been retired by Google (404 "no longer
# available to new users"). This model was selected by calling the real REST
# API GET /v1beta/models directly (the SDK version pinned in requirements.txt
# is old and its models.list() returns 501, so it can't be trusted) to get the
# actual list of model keys that really work, then testing generateContent
# directly against several candidates — other models (gemini-3.8-flash,
# gemini-3.7-flash, gemini-flash-latest...) were also in the list but
# returned 503 repeatedly during testing (most likely due to the free-tier
# key's shared capacity limits, not because the model itself is invalid —
# see retry/backoff below).
# "gemini-2.5-flash-native-audio-preview" (the old value) does not exist —
# returns 404 "not found for API version v1beta" on a real call (reproduced
# this error firsthand). Google no longer has a model named "native-audio"
# like that; the model family that supports response_modalities=["AUDIO"] +
# speech_config is now the *-tts group. Verified directly against the real
# API: listed GET /v1beta/models then tested generateContent with the real
# request shape (response_modalities=AUDIO + speech_config.voice_config)
# against the *-tts candidates.
#
# "gemini-3.8-flash-tts" (the old value, chosen on 2026-10-03) turned out to
# have a quota of generate_requests_per_model_per_day = 100/day — which CANNOT
# be raised even with billing enabled (the new TTS model appears to still have
# a low default limit, separate from the _TEXT_MODEL text model's quota). It
# burned through quota very fast because the retry logic below automatically
# retries up to 6 times per call — one dialogue line hitting a transient error
# could cost 6/100 of the quota in a single dub. Reproduced the real 429
# firsthand on Cloud Run, and verified the remaining 3 candidates still had
# quota left via real requests before switching — chose
# "gemini-3.1-flash-tts-preview" (not "lite", to keep audio quality, and a
# different line from "3.8" so it has an independent quota bucket, which had
# been exhausted).
_AUDIO_MODEL = "gemini-3.1-flash-tts-preview"
_CACHE_TTL_SECONDS = 86400

# Voice Design (client.voices.create, type="prompted") — synthesizes a NEW
# voice matching a text description, requiring NO consent_audio (unlike Voice
# Replication, which requires the actual voice owner to read a consent
# sentence aloud themselves — impossible for the original dubbing actor's
# voice in an existing video). Only 2 models support it:
# gemini-3.8-flash-tts / -flash-lite-tts (verified via
# https://ai.google.dev/gemini-api/docs/voice-design) — chose the non-"lite"
# version to prioritize quality ("studio-grade voice fidelity, expressive
# acting" per the official description).
#
# IMPORTANT — CORRECTED THE INITIAL ASSUMPTION AFTER TESTING FOR REAL: the SDK
# docstring (VoiceOutput.id) says the voice_id "can be used with ANY
# subsequent TTS model", so the initial expectation was that this model would
# only need to be called ONCE per character to create the voice_id, while the
# step that ACTUALLY SYNTHESIZES AUDIO for each dialogue line
# (synthesize_dubbed_audio, called frequently) would keep using the old
# _AUDIO_MODEL (not subject to quota). REPRODUCED THIS FIRSTHAND via a real
# request: calling the SAME just-created voice_id with _AUDIO_MODEL
# (gemini-3.1-flash-tts-preview) always returns 500 INTERNAL; calling it again
# with the CORRECT _VOICE_DESIGN_MODEL succeeds — the controlled comparison
# was repeated multiple times with consistent results. In other words, the
# SDK docstring does not hold true for the old preview model in practice. The
# consequence: synthesize_dubbed_audio MUST use _VOICE_DESIGN_MODEL for EVERY
# call when a character has a voice_id — not just the voice-creation step —
# so the 3.8 model line's generate_requests_per_model_per_day=100/day quota
# (verified firsthand via the real gcloud quota API — NOT raised even with
# billing enabled) APPLIES TO EVERY DIALOGUE LINE of a character using Voice
# Design, with no way to avoid it as originally hoped. This tradeoff is
# accepted per the actual requirement (prioritizing matching the original
# voice over quota concerns at this stage).
_VOICE_DESIGN_MODEL = "gemini-3.8-flash-tts"

# 3 attempts/~7s total (backoff 1s+2s+4s) once wasn't enough to get past a
# real burst of Gemini free-tier 503 "high demand" errors (verified
# firsthand: multiple jobs in a row failed all 3 retries while the model was
# still functioning, just temporarily overloaded) — raised to 6 attempts,
# with increasing backoff capped at 20s per attempt (worst-case total ~51s)
# instead of growing exponentially without bound.
_DEFAULT_RETRIES = 6
_MAX_BACKOFF_SECONDS = 20.0


def _backoff_seconds(attempt: int) -> float:
    return min(2**attempt, _MAX_BACKOFF_SECONDS)


# Gemini TTS returns 16-bit PCM, mono, 24kHz (no header) — see
# docs/tech-stack.md, Gemini Native Audio section. These parameters should be
# reconfirmed against a real response once an API key is available (same TODO
# group as _AUDIO_MODEL above).
_AUDIO_SAMPLE_RATE_HZ = 24000
_AUDIO_CHANNELS = 1
_AUDIO_SAMPLE_WIDTH_BYTES = 2

# List of Gemini TTS prebuilt voices + style descriptions — cross-checked
# directly against the official docs
# https://ai.google.dev/gemini-api/docs/speech-generation (2026-10-04). NOTE:
# the official docs do NOT publish a gender for each voice (only a style
# description like "Bright"/"Firm") — so matching a voice to a character's
# real gender/tone relies on Gemini ACTUALLY LISTENING to a real audio sample
# and choosing (see select_voice_for_speaker below), NOT a hardcoded
# guessed-gender table (unreliable, error-prone — see
# voice_profiling_agent.py). Multiple voices are used (instead of one fixed
# voice for every character) to keep character voices distinct throughout the
# series — see AGENTS.md principle #1, a core differentiator.
_PREBUILT_VOICE_DESCRIPTIONS = {
    "Zephyr": "Bright",
    "Puck": "Upbeat",
    "Charon": "Informative",
    "Kore": "Firm",
    "Fenrir": "Excitable",
    "Leda": "Youthful",
    "Orus": "Firm",
    "Aoede": "Breezy",
    "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",
    "Enceladus": "Breathy",
    "Iapetus": "Clear",
}
_PREBUILT_VOICE_NAMES = list(_PREBUILT_VOICE_DESCRIPTIONS.keys())

# Uses response_mime_type=application/json instead of relying only on prompt
# wording — avoids Gemini wrapping the result in a markdown code fence, which
# would break json.loads().
_JSON_CONFIG = genai_types.GenerateContentConfig(response_mime_type="application/json")


class _TranscriptionItem(BaseModel):
    """response_schema for transcribe_audio — enforces the JSON number type
    for start_time/end_time at the API layer instead of relying only on the
    wording in TRANSCRIPTION_PROMPT (see the _call_text_with_retry docstring).
    Real bug encountered: Gemini would sometimes return a "mm:ss.mmm" string
    instead of a number for start_time/end_time even though the prompt
    clearly asked for a number, causing DialogueLine(...) to raise
    ValidationError and crash the whole job (see
    ingestion_agent.py::_parse_timestamp_seconds — still kept as a second line
    of defense, since response_schema does not guarantee 100% coverage of
    every case)."""

    start_time: float
    end_time: float
    speaker_tag: str
    text: str


def _probe_local_duration_seconds(path: str) -> float:
    """Real (ffprobe-measured) duration of a local media file — used by
    transcribe_audio to ground TRANSCRIPTION_PROMPT with the video's actual
    length (see TRANSCRIPTION_DURATION_CONSTRAINT in prompts.py), so Gemini
    has a concrete fact to anchor timestamps to instead of only its own
    (sometimes wrong) perception of when the video ends."""
    return float(ffmpeg.probe(path)["format"]["duration"])


def _cache_key(*parts: str) -> str:
    raw = "|".join(parts)
    return f"gemini:{hashlib.sha256(raw.encode()).hexdigest()}"


# Languages not delimited by spaces — "word count" (text.split()) is
# meaningless in BOTH directions (source and target): a Japanese/Chinese/Thai
# sentence with no spaces yields word_count=1 (or a few stray numbers from
# punctuation/stray tokens), feeding this bogus number into the prompt makes
# Gemini try to "match" a meaningless metric instead of preserving meaning
# fully (discovered via gemini-prompt-reviewer, never reproduced for real
# since test jobs so far have all had English sources — but ja/zh/th are
# genuinely valid source languages in NewProject.tsx, not a theoretical edge
# case).
_NON_SPACE_DELIMITED_LANGS = {"ja", "zh", "zh-tw", "th", "km", "my", "lo"}

# Rough localization-industry rule-of-thumb for how much LONGER (positive) or
# SHORTER (negative) a target language typically comes out compared to the
# same English meaning — e.g. 0.20 means "typically ~20% longer than
# English". These are directional hints, not measured per-project data, used
# to tell Gemini UP FRONT to proactively compress/expand instead of
# translating literally and only discovering afterward that it doesn't fit
# the dubbing slot (this is exactly how the manual pre-tool dubbing process
# handles it — see docs/manual-dubbing-process.docx, which explicitly warns
# its translators that Spanish/French/German "naturally run 15-25% longer
# than English, so MUST compress"). A language absent from this dict is
# treated as neutral (no directional hint) — ABSENCE must never be read as
# "this language matches English's length", only as "we don't have a
# rule-of-thumb for it yet"; it falls back to the length-guidance text's
# general accuracy/naturalness instructions.
_LENGTH_EXPANSION_FROM_ENGLISH: dict[str, float] = {
    "es": 0.20,  # Spanish
    "fr": 0.18,  # French
    "de": 0.20,  # German (compounding makes this highly variable; kept moderate)
    "pt": 0.20,  # Portuguese
    "id": 0.15,  # Indonesian
    "ms": 0.15,  # Malay
    "tl": 0.15,  # Tagalog/Filipino
    "vi": 0.05,  # Vietnamese
    "ja": -0.15,  # Japanese — dense script, usually shorter in character count
    "ko": -0.05,  # Korean
    "zh": -0.25,  # Chinese (Simplified) — very dense in character count
    "zh-tw": -0.25,  # Chinese (Traditional)
    "th": -0.10,  # Thai
    "hi": 0.15,  # Hindi
    "bn": 0.15,  # Bengali
    "ta": 0.15,  # Tamil
    "te": 0.15,  # Telugu
    "ur": 0.15,  # Urdu
    "km": 0.05,  # Khmer
    "my": 0.05,  # Burmese
    "lo": 0.05,  # Lao
}

# Below this magnitude, a language's expansion/contraction tendency is too
# small (or too uncertain) to be worth calling out explicitly — avoids
# cluttering the prompt with a directional hint for languages that are
# roughly comparable to English in length.
_LENGTH_EXPANSION_NOTE_THRESHOLD = 0.1


def _build_length_expansion_note(source_lang: str, target_lang: str) -> str:
    """A target-language-specific nudge appended to the length guidance, see
    _LENGTH_EXPANSION_FROM_ENGLISH above. Kept as a separate function (not
    inlined into _build_length_guidance) so the 2 return branches there don't
    have to duplicate this logic.

    Gated on source_lang == "en" (found via gemini-prompt-reviewer, review
    2026-10-10): _LENGTH_EXPANSION_FROM_ENGLISH is a rule-of-thumb for "vs
    ENGLISH" specifically (see docs/manual-dubbing-process.docx) — it says
    nothing about how e.g. Indonesian compares in length to a Vietnamese or
    Japanese source. Non-English source languages are a real pipeline path
    (see SOURCE_LANGUAGES in NewProject.tsx: vi/ja/ko/zh/en), so applying an
    English-relative percentage there would be an unfounded, possibly
    backwards, directional hint. Treated the same as "no rule-of-thumb for
    this language yet": no note at all, falls back to the general
    accuracy/naturalness instructions."""
    if source_lang.lower() != "en":
        return ""
    expansion = _LENGTH_EXPANSION_FROM_ENGLISH.get(target_lang.lower(), 0.0)
    if expansion >= _LENGTH_EXPANSION_NOTE_THRESHOLD:
        return (
            f" {target_lang} typically comes out about {round(expansion * 100)}% "
            "LONGER than English for the same meaning — actively COMPRESS "
            "rather than translating word-for-word: drop the subject when the "
            "spoken style allows it, use contracted/short forms, pick shorter "
            "synonyms."
        )
    if expansion <= -_LENGTH_EXPANSION_NOTE_THRESHOLD:
        return (
            f" {target_lang} typically comes out about {round(abs(expansion) * 100)}% "
            "SHORTER than English for the same meaning — do not artificially "
            "pad the line with extra words just to fill space."
        )
    return ""


def _build_length_guidance(text: str, source_lang: str, target_lang: str) -> str:
    """Decides whether to use word count or character count to guide
    translation length — this decision is a linguistic/technical fact (which
    languages are space-delimited), not a business rule that varies by
    market, so hardcoding it in code is reasonable (unlike
    cultural_safety_rules — see AGENTS.md principle #6); it does NOT rely on
    Gemini figuring this out from a note in the prompt (too easy to miss or
    apply in the wrong direction).

    Tolerance tightened from ±20% to ±10% (review 2026-10-10, comparing
    against docs/manual-dubbing-process.docx): the manual process this tool
    replaces holds translators to ±10% syllable count, explicitly treating a
    ±20%-equivalent deviation as unacceptable. ±20% left too much slack for
    the downstream atempo time-stretch step (mux_service.py) to absorb
    without sounding sped-up/slowed-down."""
    char_count = len(text)
    space_delimited = (
        source_lang.lower() not in _NON_SPACE_DELIMITED_LANGS
        and target_lang.lower() not in _NON_SPACE_DELIMITED_LANGS
    )
    expansion_note = _build_length_expansion_note(source_lang, target_lang)
    if space_delimited:
        word_count = len(text.split())
        return (
            f"the original line has about {word_count} words ({char_count} "
            "characters). Among phrasings that are NATURAL, ACCURATE, and "
            "CHILD-APPROPRIATE, choose the one whose WORD COUNT and CHARACTER "
            f"COUNT are CLOSEST to the original (a ±10% deviation is acceptable)."
            f"{expansion_note}"
        )
    return (
        f"the original line has about {char_count} characters (one of "
        f"{source_lang}/{target_lang} is not space-delimited, so WORD COUNT "
        "isn't a reliable metric here — use CHARACTER COUNT only). Among "
        "phrasings that are NATURAL, ACCURATE, and CHILD-APPROPRIATE, choose "
        "the one whose CHARACTER COUNT is CLOSEST to the original (a ±10% "
        f"deviation is acceptable).{expansion_note}"
    )


def list_prebuilt_voices() -> list[VoiceOption]:
    """The 12 prebuilt TTS voices + their official style description, for a
    human to pick from directly (specs/003-script-first-ingestion, see
    GET /characters/voice-options) when there's no audio sample for Gemini to
    listen to and match automatically (see select_voice_for_speaker below).
    Returns the SAME set _PREBUILT_VOICE_NAMES/voice_name_for_character
    already use internally, just exposed as a small public, read-only list
    instead of leaving the API layer reach into a private module attribute
    directly. Returns VoiceOption (not a raw dict) per AGENTS.md's "no raw
    dicts between layers" — the API router can return this list as-is."""
    return [
        VoiceOption(voice_name=name, description=description)
        for name, description in _PREBUILT_VOICE_DESCRIPTIONS.items()
    ]


def voice_name_for_character(character_id: str | None) -> str:
    """Stable mapping of character_id -> a Gemini prebuilt voice, so the same
    character always gets the same voice throughout the series instead of
    every character sharing one voice (see AGENTS.md principle #1)."""
    if character_id is None:
        return _PREBUILT_VOICE_NAMES[0]
    index = int(hashlib.sha256(character_id.encode()).hexdigest(), 16) % len(_PREBUILT_VOICE_NAMES)
    return _PREBUILT_VOICE_NAMES[index]


def _is_wav_container(data: bytes) -> bool:
    """True if `data` is already a complete WAV file (has a RIFF/WAVE
    header) — see _pcm_to_wav for why this distinction matters."""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    """Wraps raw PCM (no header) into a valid WAV file.

    IMPORTANT: only call this function when `pcm_bytes` is ACTUALLY raw PCM
    with no header yet — verified firsthand via a real request to the
    current TTS model (`_AUDIO_MODEL`): the response returns
    `inline_data.mime_type="audio/wav"` and the data STARTS WITH real magic
    bytes "RIFF....WAVE", meaning Gemini already packages it as a complete
    WAV, not bare raw PCM as the old comment assumed. If this function is
    still called on data that is already WAV, the result is a WAV file
    NESTED INSIDE another WAV file — the outer header correctly declares its
    structure, but its "data" chunk actually contains the inner file's RIFF
    header too, which gets misinterpreted as PCM samples (the first 44 bytes
    turn into noise, and the rest is completely offset from its real
    meaning). This is EXACTLY the root cause of the "video crash" bug in
    ReviewDashboard.tsx: ffmpeg/ffprobe report no error at all (raw PCM — any
    byte sequence is "structurally valid", there's no concept of "invalid
    PCM"), but CHROME's audio renderer refuses to play it after this
    nested-WAV file gets further processed through atempo/amix/AAC encoding
    (see mux_service.py) — reproduced the real error firsthand via
    chrome://media-internals: "AUDIO_RENDERER_ERROR". See where this function
    is called (synthesize_dubbed_audio) — it MUST check _is_wav_container()
    first, and only call _pcm_to_wav when that's False."""
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_AUDIO_CHANNELS)
        wav_file.setsampwidth(_AUDIO_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(_AUDIO_SAMPLE_RATE_HZ)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


class GeminiClient:
    def __init__(self, api_key: str, redis: RedisClient):
        self._api_key = api_key
        self._redis = redis
        # Lazy — genai.Client() raises immediately if api_key is empty/unset.
        # Deferred until the first real call so importing the module (e.g.
        # worker/main.py at module level) doesn't require GEMINI_API_KEY to
        # already be set (e.g. running tests/lint locally) — same reasoning
        # as worker/services/gcs_client.py.
        self._client_instance: genai.Client | None = None

    @property
    def _client(self) -> genai.Client:
        if self._client_instance is None:
            self._client_instance = genai.Client(api_key=self._api_key)
        return self._client_instance

    async def _call_text_with_retry(
        self,
        contents: str | list,
        retries: int = _DEFAULT_RETRIES,
        json_response: bool = False,
        response_schema: object | None = None,
    ) -> str:
        """google-genai's sync client makes a blocking network call — runs it in
        a thread pool so it doesn't block the event loop (see AGENTS.md
        backend: all I/O must be async). `contents` accepts either plain
        prompt text (translation, safety review...) or a list
        [Part, prompt] for multimodal input (transcribe_audio — see below).

        `response_schema` (optional, e.g. `list[SomePydanticModel]`): enforces
        the data TYPE of each field at the API layer instead of relying only
        on the prompt's wording — a real bug encountered (transcribe_audio
        returning start_time/end_time as a "mm:ss.mmm" string instead of a
        number even though the prompt clearly asked for one, see
        ingestion_agent.py::_parse_timestamp_seconds) showed that prompt
        wording is not reliable enough for a data TYPE constraint.
        `_JSON_CONFIG` (a constant shared by call sites that don't need their
        own schema) isn't enough here — each call site with a different
        schema must build its own config here."""
        last_error: Exception | None = None
        if response_schema is not None:
            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=response_schema
            )
        elif json_response:
            config = _JSON_CONFIG
        else:
            config = None
        for attempt in range(retries):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=_TEXT_MODEL,
                    contents=contents,
                    config=config,
                )
                return response.text
            except Exception as exc:  # noqa: BLE001 - retry every transient error
                last_error = exc
                if attempt < retries - 1:
                    await asyncio.sleep(_backoff_seconds(attempt))
        raise RuntimeError(f"Gemini call failed after {retries} retries") from last_error

    async def _upload_file_with_retry(self, local_path: str, retries: int = _DEFAULT_RETRIES):
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                # google-genai >= 2.x renamed the parameter from path= to
                # file= (verified via inspect.signature on the real SDK
                # 2.28.0 — the old SDK 0.3.0 this project previously pinned
                # used path=; upgraded the SDK to be able to use
                # client.voices/client.interactions for Voice Design, see
                # design_voice_from_sample below).
                return await asyncio.to_thread(self._client.files.upload, file=local_path)
            except Exception as exc:  # noqa: BLE001 - retry every transient error
                last_error = exc
                if attempt < retries - 1:
                    await asyncio.sleep(_backoff_seconds(attempt))
        raise RuntimeError(f"Gemini file upload failed after {retries} retries") from last_error

    async def _wait_until_file_active(
        self, uploaded_file, retries: int = 90, interval_seconds: float = 2.0
    ):
        """A newly uploaded file stays in PROCESSING state for a while before
        it can be used in generateContent (verified firsthand against the
        real API — a short audio file of a few seconds becomes ACTIVE right
        away, but a longer video may need to wait).

        retries=90 (180s max wait) — previously 15 (only 30s), which caused a
        real failure when testing with a video close to 3 minutes long: the
        file was still PROCESSING after 30s, so it raised too early, leaving
        the job stuck forever at its old stage (there's no automatic
        retry/resume mechanism for jobs, see worker/pipeline.py). Gemini's
        file-processing time scales with media length, and 30s was only
        enough for clips a few dozen seconds long."""
        for _ in range(retries):
            if uploaded_file.state == "ACTIVE":
                return uploaded_file
            if uploaded_file.state == "FAILED":
                raise RuntimeError(f"Gemini failed to process file: {uploaded_file.error}")
            await asyncio.sleep(interval_seconds)
            uploaded_file = await asyncio.to_thread(self._client.files.get, name=uploaded_file.name)
        raise RuntimeError("Gemini file did not become ACTIVE within the wait time")

    async def transcribe_audio(self, local_video_path: str) -> list[dict]:
        """Used by Agent 1 — returns a raw list of dialogue lines (no
        id/character_id yet).

        Uploads the real video to the Gemini File API then references it in
        generateContent via Part.from_uri — the previous version only sent
        plain prompt text, with NO real video/audio attached, so Gemini had
        to "guess" the content, producing a transcript that didn't match the
        real video at all (reproduced firsthand while testing on the cloud:
        the transcript looked like mock data; verified the fix firsthand with
        a real request — see test_gemini_client.py). The input is a local
        file path (already downloaded from GCS in IngestionAgent, see
        worker/agents/ingestion_agent.py) because the Gemini Developer API
        (which uses an API key, unlike Vertex AI) cannot read directly from a
        gs:// URI.

        Deletes the uploaded file right after use (Gemini auto-expires it
        after 48h anyway, but there's no need to keep it, to avoid wasting
        storage on Gemini's side).

        Grounds the prompt with the file's REAL (ffprobe-measured) duration
        when available (see TRANSCRIPTION_DURATION_CONSTRAINT in prompts.py)
        — a mitigation, not a guarantee, for a real bug found via feedback:
        Gemini once assigned a line an end_time of 102.5s for a video that
        was actually only 65.8s long. Probing is best-effort: if it fails
        (e.g. an unusual container ffprobe can't read metadata from), the
        call proceeds WITHOUT the constraint rather than failing the whole
        transcription over a prompt-quality nicety — see
        ingestion_agent.py::_drop_lines_past_video_duration for the
        deterministic backstop that still catches this even without it."""
        prompt_text = prompts.TRANSCRIPTION_PROMPT
        try:
            duration_seconds = await asyncio.to_thread(
                _probe_local_duration_seconds, local_video_path
            )
            prompt_text += prompts.TRANSCRIPTION_DURATION_CONSTRAINT.format(
                duration_seconds=round(duration_seconds, 1)
            )
        except Exception:  # noqa: BLE001 - best-effort prompt grounding, see docstring above
            logger.warning(
                "transcribe_audio: could not probe the real duration of %s — "
                "proceeding without the duration-bound prompt constraint",
                local_video_path,
            )

        uploaded = await self._upload_file_with_retry(local_video_path)
        uploaded = await self._wait_until_file_active(uploaded)
        part = genai_types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)
        try:
            raw = await self._call_text_with_retry(
                [part, prompt_text],
                json_response=True,
                response_schema=list[_TranscriptionItem],
            )
        finally:
            await asyncio.to_thread(self._client.files.delete, name=uploaded.name)
        return json.loads(raw)

    async def select_voice_for_speaker(self, local_audio_sample_path: str) -> dict:
        """Used by Agent 2 — listens to a real audio sample of a speaker (cut
        from the original video, see voice_profiling_agent.py) and picks the
        closest-matching prebuilt TTS voice (gender/pitch/style). The
        previous version only assigned a voice via hash(character_id) —
        completely random, never listening to the original audio, which
        could pick a voice of a completely different gender than the real
        character (received real feedback firsthand during a real review on
        Cloud Run: "voice changed from female to male"). `voice_name_for_character`
        (the hash) is still kept as a fallback for when this step fails or
        Gemini returns an invalid (hallucinated) voice name — a secondary
        step's failure must not fail the whole job.

        Returns {"voice_name": <a valid name from _PREBUILT_VOICE_NAMES> |
        None, "style_description": <2-4 short keywords, used directly in the
        TTS instruction — see DUBBING_AUDIO_INSTRUCTION>, "reasoning": <full
        explanation, shown only to the human reviewer>}. voice_name=None
        means the hash fallback should be used — this branch has NO
        style_description, the caller handles its own fallback (see
        voice_profiling_agent.py::build_voice_profile_metadata)."""
        uploaded = await self._upload_file_with_retry(local_audio_sample_path)
        uploaded = await self._wait_until_file_active(uploaded)
        part = genai_types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)
        voice_options = "\n".join(
            f"- {name}: {desc}" for name, desc in _PREBUILT_VOICE_DESCRIPTIONS.items()
        )
        prompt = prompts.VOICE_SELECTION_PROMPT.format(voice_options=voice_options)
        try:
            raw = await self._call_text_with_retry([part, prompt], json_response=True)
        finally:
            await asyncio.to_thread(self._client.files.delete, name=uploaded.name)

        result = json.loads(raw)
        if result.get("voice_name") not in _PREBUILT_VOICE_NAMES:
            return {"voice_name": None, "reasoning": "Gemini returned an invalid voice name"}
        return result

    async def describe_voice_from_sample(self, local_audio_sample_path: str) -> str:
        """Step 1/2 of Voice Design (see design_voice below) — listens to a
        real audio sample then writes a detailed description of the voice's
        characteristics in English, used as input for the voice-synthesis
        step. Raises on error — the caller
        (voice_profiling_agent.py::build_voice_profile_metadata) falls back
        to select_voice_for_speaker (picking among the 12 prebuilt voices)
        when this step or design_voice fails, so a secondary step's failure
        doesn't fail the whole job."""
        uploaded = await self._upload_file_with_retry(local_audio_sample_path)
        uploaded = await self._wait_until_file_active(uploaded)
        part = genai_types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)
        try:
            raw = await self._call_text_with_retry(
                [part, prompts.VOICE_DESCRIPTION_PROMPT], json_response=True
            )
        finally:
            await asyncio.to_thread(self._client.files.delete, name=uploaded.name)
        return json.loads(raw)["description"]

    async def design_voice(self, description: str, display_name: str) -> str:
        """Step 2/2 of Voice Design — synthesizes a NEW voice matching the
        text description (see _VOICE_DESIGN_MODEL for why this does NOT
        require consent_audio like Voice Replication). Returns a voice_id
        (shaped like "voice_..."), used in
        SpeechConfig.voice_config.voice — but ONLY works with the exact
        _VOICE_DESIGN_MODEL (verified firsthand: _AUDIO_MODEL returns 500
        INTERNAL with a voice_id even though the SDK docstring says it works
        with any model, see _VOICE_DESIGN_MODEL for the controlled-test
        details) — synthesize_dubbed_audio automatically picks the right
        model based on whether a voice_id is present, the caller doesn't
        need to handle this itself.

        `store=True`: persists the voice long-term tied to the project (up
        to 1 year since last use, auto-renewed on every synthesize call — see
        the SDK's real docstring, VoiceOutput.expire_time) — appropriate
        since Character is a long-lived entity in the DB, not a use-once-and-
        discard thing.

        Raises on error — the caller handles its own fallback (see
        describe_voice_from_sample)."""
        result = await asyncio.to_thread(
            self._client.voices.create,
            voice={
                "type": "prompted",
                "model": _VOICE_DESIGN_MODEL,
                "display_name": display_name,
                "prompted": {"input": description},
            },
            store=True,
        )
        return result.id

    async def translate_dialogue(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        is_song: bool,
        context: str = "",
    ) -> str:
        """Used by Agent 3 — 2 modes: regular translation / song
        transcreation.

        The regular-translation prompt is given the source text's length
        (word count/char count — something Gemini can actually count/verify,
        far more reliable than asking it to infer speaking speed from a
        duration in seconds, see _build_length_guidance) to prioritize a
        phrasing whose length is close to the original — previously this
        information wasn't provided, so the translation could come out
        abnormally long/short, dumping the entire job of "forcing it to fit
        the duration" onto the later TTS + ffmpeg time-stretch step (atempo),
        which sounded like it had been sped up/slowed down abnormally, with
        audio overlapping between lines (received this feedback firsthand
        during a real review on Cloud Run — the first version, which used
        duration_seconds, still wasn't tight enough, so that parameter was
        dropped entirely since the word/char count computed directly from
        `text` is sufficient, with no more need to know duration_seconds).

        `context` (used only for regular translation, not for song
        transcreation): the lines adjacent to `text`, built by
        TranslationAgent (see translation_agent.py:_build_context) —
        dialogue_lines are automatically split by timestamp, so one complete
        sentence can end up split across multiple lines; translating each
        line completely independently (without this context) gets the
        GRAMMAR of that individual line right but the MEANING of the whole
        sentence wrong once reassembled (received real feedback: "splitting
        the sentence into pieces broke the meaning of the whole sentence").

        The cache key also includes `context` — a different context (different
        surrounding lines) can translate to a different meaning, so it must
        not be cached together with identical text under a different
        context."""
        cache_key = _cache_key("translate", text, source_lang, target_lang, str(is_song), context)
        if cached := await self._redis.get(cache_key):
            return cached

        if is_song:
            prompt = prompts.SONG_TRANSCREATION_PROMPT.format(
                text=text, source_lang=source_lang, target_lang=target_lang
            )
        else:
            # `context or f">>> {text}"`: a defensive default for a caller
            # invoking translate_dialogue directly without going through
            # TranslationAgent (e.g. a standalone script/test) — in the real
            # pipeline, _build_context always returns at least the line being
            # translated itself (its range always includes its own index), so
            # this fallback branch is NOT a real code path in the pipeline;
            # it just ensures translate_dialogue never receives an empty
            # context that would leave the prompt missing its context section.
            prompt = prompts.DIALOGUE_TRANSLATION_PROMPT.format(
                text=text,
                source_lang=source_lang,
                target_lang=target_lang,
                length_guidance=_build_length_guidance(text, source_lang, target_lang),
                context=context or f">>> {text}",
            )
        result = await self._call_text_with_retry(prompt)

        await self._redis.set(cache_key, result, ttl=_CACHE_TTL_SECONDS)
        return result

    async def check_cultural_safety(self, text: str, target_market: str, market_rules: str) -> dict:
        """Used by Agent 4 — returns {"safety_status", "reasoning"}."""
        prompt = prompts.CULTURAL_SAFETY_PROMPT.format(
            text=text, target_market=target_market, market_rules=market_rules
        )
        raw = await self._call_text_with_retry(prompt, json_response=True)
        return json.loads(raw)

    async def synthesize_dubbed_audio(
        self,
        dialogue_line_id: str,
        text: str,
        target_lang: str,
        voice_style: str,
        character_id: str | None,
        voice_name: str | None = None,
        voice_id: str | None = None,
    ) -> bytes:
        """Used by Agent 5 — calls Gemini Native Audio, returns audio bytes as
        valid WAV (header already packaged, see `_pcm_to_wav`).

        `voice_id`: a voice synthesized specifically via Voice Design (see
        design_voice), matching the original voice's characteristics far
        better than the 12 prebuilt voices — PREFERRED when available.
        `voice_name`: a prebuilt voice already chosen by Agent 2 by listening
        to a real audio sample (see select_voice_for_speaker +
        voice_profiling_agent.py) — the fallback when there's no voice_id. If
        neither is present, falls back to hash(character_id) as in the old
        version (e.g. a character created before the Voice Design/profiling
        feature existed) — a missing voice must not fail the whole dubbing
        step.

        No longer accepts `duration_seconds` (dropped from the previous
        version) — Gemini TTS has no parameter that directly controls
        duration in seconds, and stuffing that requirement into the prompt
        only gave the model more content to "interpret", which was part of
        the cause of the "dubbed audio contains the instruction sentence"
        bug (see prompts.py:DUBBING_AUDIO_INSTRUCTION). Duration is handled
        properly at the time-stretch step after muxing (mux_service.py).

        `voice_style` is ONLY stuffed into the instruction (via
        DUBBING_AUDIO_INSTRUCTION) when using a PREBUILT voice (no voice_id)
        — a Voice Design voice (voice_id) has ALREADY been synthesized to
        match the original voice's description (see
        voice_profiling_agent.py::build_voice_profile_metadata, layer 1), so
        adding a "Speak in a ... voice:" sentence here is both redundant and
        exactly the cause of a real bug encountered (reported 2026-10): EVERY
        character using Voice Design was assigned the exact same hardcoded
        `style_description` ("neutral, clear" — see voice_profiling_agent.py,
        a value that doesn't describe the real voice's characteristics at
        all), and the TTS model would then read that sentence aloud at the
        START OF EVERY dialogue line (exactly the "contents sent in are
        treated as a transcript to read verbatim" bug documented in
        prompts.py). Using `text` directly WITHOUT the template for the
        voice_id branch eliminates this risk entirely, rather than just
        changing the wording of the instruction sentence (which would leave
        the underlying mechanism that causes the bug intact)."""
        instruction = (
            text
            if voice_id
            else prompts.DUBBING_AUDIO_INSTRUCTION.format(text=text, voice_style=voice_style)
        )
        # The cache key includes `instruction` (the real content sent to
        # Gemini), NOT just (dialogue_line_id, target_lang, voice_name) as in
        # the old version — a real bug encountered: DUBBING_AUDIO_INSTRUCTION
        # was fixed (removing the long instruction sentence that caused the
        # TTS to misread it aloud), but a dialogue line already dubbed before
        # with the SAME voice_name still returned the OLD audio straight from
        # the cache (generated from the old prompt); the user clicked
        # "Redub" and heard the exact same bug even though the code had
        # been fixed correctly. Including `instruction` in the key means ANY
        # change to the prompt/text/voice_style automatically produces a new
        # cache key, with no need to remember to manually clear/bump the
        # cache version every time the prompt changes going forward. Also
        # added voice_id to the key (same reasoning as voice_name) — changing
        # the Voice Design voice while voice_name stays the same (None) must
        # not incorrectly cache-hit.
        cache_key = _cache_key(
            "dub_audio",
            dialogue_line_id,
            target_lang,
            voice_id or "",
            voice_name or "",
            instruction,
        )
        if cached := await self._redis.get(cache_key):
            return base64.b64decode(cached)

        if voice_id:
            # "voice" (NOT prebuilt_voice_config) — the shared field that
            # accepts a Voice Design/Replication voice_id, verified via the
            # SDK's real docstring (see design_voice: VoiceOutput.id "Pass
            # {id} directly to SpeechConfig.voice_config.voice... during
            # speech synthesis").
            #
            # MUST use _VOICE_DESIGN_MODEL (gemini-3.8-flash-tts), NOT
            # _AUDIO_MODEL (gemini-3.1-flash-tts-preview) like the prebuilt
            # branch below — even though the SDK docstring says voice_id
            # "works with ANY TTS model", verified firsthand via a real
            # request: calling this voice_id with _AUDIO_MODEL always returns
            # 500 INTERNAL (a controlled test with the same voice_id, only
            # the model changed — _VOICE_DESIGN_MODEL succeeds,
            # _AUDIO_MODEL fails 100% of attempts). The old (preview) model
            # doesn't actually seem to support the voice_id mechanism despite
            # the SDK listing it as generally supported. Consequence:
            # characters using Voice Design WILL be stuck with the 3.8 model
            # line's generate_requests_per_model_per_day=100/day quota (see
            # _VOICE_DESIGN_MODEL) for EVERY dialogue line of that character,
            # with no way to avoid it as originally hoped.
            model = _VOICE_DESIGN_MODEL
            voice_config = genai_types.VoiceConfig(voice=voice_id)
        else:
            model = _AUDIO_MODEL
            voice_name = voice_name or voice_name_for_character(character_id)
            voice_config = genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        audio_config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(voice_config=voice_config),
        )
        retries = _DEFAULT_RETRIES
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=model,
                    contents=instruction,
                    config=audio_config,
                )
                part = response.candidates[0].content.parts[0]
                audio_bytes = part.inline_data.data
                if not audio_bytes:
                    raise RuntimeError("Gemini Native Audio returned an empty response")
                # The current TTS model returns a complete WAV (a real RIFF
                # header, mime_type="audio/wav"), NOT bare raw PCM as
                # previously assumed — wrapping it in another WAV layer
                # produces a WAV file nested inside a WAV (see _pcm_to_wav
                # for details + how this bug was discovered firsthand). Only
                # wrap it when it's ACTUALLY raw PCM.
                wav_bytes = (
                    audio_bytes if _is_wav_container(audio_bytes) else _pcm_to_wav(audio_bytes)
                )
                await self._redis.set(
                    cache_key, base64.b64encode(wav_bytes).decode(), ttl=_CACHE_TTL_SECONDS
                )
                return wav_bytes
            except Exception as exc:  # noqa: BLE001 - retry every transient error
                last_error = exc
                if attempt < retries - 1:
                    await asyncio.sleep(_backoff_seconds(attempt))
        raise RuntimeError(
            f"Gemini Native Audio call failed after {retries} retries"
        ) from last_error

    async def generate_educational_content(self, full_transcript: str, target_lang: str) -> dict:
        """Used by Agent 7 — returns {"vocabulary": [...], "quiz": [...]}."""
        prompt = prompts.EDUCATIONAL_CONTENT_PROMPT.format(
            full_transcript=full_transcript, target_lang=target_lang
        )
        raw = await self._call_text_with_retry(prompt, json_response=True)
        return json.loads(raw)
