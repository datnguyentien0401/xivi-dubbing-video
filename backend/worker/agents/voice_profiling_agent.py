"""Agent 2 — Character Voice Profiling (see docs/agent-pipeline.md).

Input: context.dialogue_lines (has speaker_tag), context.characters (saved
       from a previous episode, if any)
Output: each dialogue_line gets a consistent character_id assigned; each NEW
Character gets a voice_name (TTS voice) assigned based on a real audio sample
of that speaker.
"""

import os
import tempfile
import uuid

import ffmpeg

from worker.models.pipeline_context import Character, DialogueLine, PipelineContext
from worker.services.gemini_client import GeminiClient
from worker.services.storage import StorageClient


def _pick_sample_line(dialogue_lines: list[DialogueLine], tag: str) -> DialogueLine:
    """Pick this speaker's LONGEST dialogue line as the voice sample — a
    longer line gives a clearer signal for Gemini to detect
    gender/pitch/style compared to a line that's too short (e.g. "Uh.")."""
    candidates = [line for line in dialogue_lines if line.speaker_tag == tag]
    return max(candidates, key=lambda line: line.end_time - line.start_time)


_DEFAULT_STYLE_DESCRIPTION = "neutral, clear"
# ONLY layer 2 (prebuilt voice) uses _DEFAULT_STYLE_DESCRIPTION as a fallback
# when Gemini doesn't return a style_description — layer 1 (Voice Design,
# voice_id) no longer sets this field at all (see the comment in the return
# dict below): a Voice Design voice already matches the original voice's
# characteristics, and GeminiClient.synthesize_dubbed_audio completely
# ignores voice_style whenever a voice_id is present — setting one identical
# hardcoded placeholder here for EVERY character used to be a real reported
# bug: every dialogue line of every character using Voice Design started with
# "Speak in a neutral, clear voice:" because the TTS read the instruction
# sentence out loud.


async def build_voice_profile_metadata(
    gemini: GeminiClient, local_sample_path: str, display_name: str
) -> dict:
    """Listen to a local audio sample and return voice_profile_metadata —
    this logic is shared between the automatic flow (the loop below, samples
    cut from the source video) and the flow where a user manually uploads a
    voice sample (see api/routers/characters.py::classify_voice_sample,
    called via worker/main.py::_classify_character_voice_sample).

    Priority order (3 fallback layers, a failure at any layer must NOT be
    raised outward — a secondary step must not fail the whole job/request):
    1. Voice Design (gemini.describe_voice_from_sample + gemini.design_voice):
       synthesizes a NEW voice matching the original voice's characteristics
       — not limited to the 12 prebuilt voices like before, matches the
       original voice far better. Returns `voice_id`.
    2. Step 1 fails -> select_voice_for_speaker (pick from the 12 prebuilt
       voices, the old approach) -> returns `voice_name`.
    3. Both steps above fail -> both voice_id and voice_name are None ->
       synthesize_dubbed_audio falls back to hash(character_id) on its own.

    IMPORTANT about layer 1: Google itself blocks (returns an error, not a
    code bug) requests for Voice Design to synthesize a voice that sounds
    like a child/minor — which makes sense, since this is one of Google's
    own safety policies (limiting the risk of child voice synthesis abuse).
    VOICE_DESCRIPTION_PROMPT describes the voice HONESTLY (even when it is a
    child's voice — which matches the nature of most of XIVI's
    children's animated characters), and MUST NOT be altered to dodge this
    policy — if Google blocks it, let that error fall naturally into the
    `except` branch below, and fall back to layer 2 (the 12 prebuilt voices,
    already approved by Google as safe for all ages)."""
    try:
        description = await gemini.describe_voice_from_sample(local_sample_path)
        voice_id = await gemini.design_voice(description, display_name=display_name)
        # Do NOT set "style_description" here (real reported bug: this used
        # to hardcode the identical "neutral, clear" for EVERY character
        # — see the comment on _DEFAULT_STYLE_DESCRIPTION above) — a Voice
        # Design voice (voice_id) already matches the original voice's
        # characteristics from `description`, and
        # GeminiClient.synthesize_dubbed_audio completely ignores voice_style
        # whenever a voice_id is present, so this field is meaningless in
        # this branch.
        return {
            "voice_id": voice_id,
            "voice_description": description,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort, has a fallback at layer 2/3
        voice_design_error = str(exc)

    try:
        selection = await gemini.select_voice_for_speaker(local_sample_path)
    except Exception as exc:  # noqa: BLE001 - best-effort, has a hash fallback
        selection = {
            "voice_name": None,
            "reasoning": (
                f"Voice Design failed ({voice_design_error}); selecting a "
                f"prebuilt voice also failed: {exc}"
            ),
        }

    # style_description MUST come specifically from the style_description
    # field (2-4 short keywords, see VOICE_SELECTION_PROMPT) — it must NOT
    # use the `reasoning` field (a full explanation, potentially long) as the
    # style_description like the old version did: DubbingAgent stuffs
    # style_description directly into DUBBING_AUDIO_INSTRUCTION for the TTS
    # to read, and a long explanatory sentence there reproduces exactly the
    # real reported bug "the redubbed audio also contains the instruction
    # sentence" — the only difference here is the long sentence would come
    # from reasoning instead of the hardcoded clause in the old prompt.
    return {
        "voice_name": selection.get("voice_name"),
        "voice_selection_reasoning": selection.get("reasoning"),
        "voice_design_error": voice_design_error,
        "style_description": selection.get("style_description") or _DEFAULT_STYLE_DESCRIPTION,
    }


def _extract_audio_clip(source_video_path: str, start: float, end: float, output_path: str) -> None:
    """Cut exactly the [start, end] segment on source_video_path's original
    timeline into a separate audio file — `-ss`/`-to` placed BEFORE `-i`
    (input seeking) are interpreted relative to the ABSOLUTE timestamps of
    the original file, matching start_time/end_time of dialogue_lines
    directly (no need to subtract an offset ourselves)."""
    (
        ffmpeg.input(source_video_path, ss=start, to=end)
        .output(output_path, vn=None, acodec="pcm_s16le", ar=24000, ac=1)
        .overwrite_output()
        .run(quiet=True)
    )


class VoiceProfilingAgent:
    def __init__(self, gemini: GeminiClient, gcs: StorageClient):
        self._gemini = gemini
        self._gcs = gcs

    async def run(self, context: PipelineContext) -> PipelineContext:
        # Group dialogue_lines by speaker_tag to process each speaker once
        speaker_tags = {line.speaker_tag for line in context.dialogue_lines if line.speaker_tag}

        existing_by_tag = {c.speaker_tag_original: c for c in context.characters}
        new_tags = [tag for tag in speaker_tags if tag not in existing_by_tag]

        if new_tags:
            with tempfile.TemporaryDirectory(prefix=f"voiceprofile-{context.job_id}-") as tmp_dir:
                source_video_path = os.path.join(tmp_dir, "source.mp4")
                await self._gcs.download_to_path(context.source_video_gcs_uri, source_video_path)

                for tag in new_tags:
                    character = Character(
                        id=str(uuid.uuid4()), name=f"Character ({tag})", speaker_tag_original=tag
                    )

                    sample_line = _pick_sample_line(context.dialogue_lines, tag)
                    sample_path = os.path.join(tmp_dir, f"sample-{character.id}.wav")
                    _extract_audio_clip(
                        source_video_path, sample_line.start_time, sample_line.end_time, sample_path
                    )

                    blob_name = f"jobs/{context.job_id}/voice-samples/{character.id}.wav"
                    character.voice_sample_gcs_uri = await self._gcs.upload_from_path(
                        sample_path, blob_name
                    )

                    # Listen to the real audio sample and pick the closest
                    # matching TTS voice (gender/pitch/style) — the old
                    # version only assigned a voice via hash(character_id),
                    # completely unrelated to the character's real voice (we
                    # personally received real feedback during review: "the
                    # voice changed from female to male"). The voice
                    # selection/fallback logic is shared with the flow where
                    # a user manually uploads a sample — see
                    # build_voice_profile_metadata (Gemini Native Audio is a
                    # voice-generation model, not a fixed voiceprint — we
                    # personally verified that seed/temperature do NOT make
                    # the output deterministic, so we can't guarantee an
                    # absolutely identical match, only guide it to be more
                    # consistent).
                    character.voice_profile_metadata = await build_voice_profile_metadata(
                        self._gemini, sample_path, display_name=character.name
                    )

                    context.characters.append(character)
                    existing_by_tag[tag] = character

        for line in context.dialogue_lines:
            if line.speaker_tag in existing_by_tag:
                line.character_id = existing_by_tag[line.speaker_tag].id

        context.current_stage = "voice_profiled"
        return context
