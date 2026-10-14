"""Agent 1 (script-first variant) — English Timing Benchmark
(specs/003-script-first-ingestion, see docs/manual-dubbing-process.docx's own
"English Renvoice is the duration benchmark" rule, which this mirrors).

Input: context.dialogue_lines (original_text only — no real recording exists
       yet for a script-first job, see api/routers/projects.py::create_script_jobs,
       which sets each line's start_time/end_time to a sequential 1s-per-line
       placeholder before this agent ever runs)
Output: each dialogue_line gets a REAL start_time/end_time, derived from the
        measured duration of a throwaway English voiceover Gemini generates
        for it — standing in for "the real video's measured audio" that
        IngestionAgent would normally provide for a video-first job.

Occupies the SAME "transcribed" stage slot IngestionAgent occupies for a
video-first job (see worker/pipeline.py, which picks between the two at run
time based on whether context.source_video_gcs_uri is set) — intentionally a
SEPARATE agent, not a branch bolted onto IngestionAgent, per AGENTS.md
principle #2 ("one responsibility per agent"): IngestionAgent's responsibility
is "transcribe from a real recording"; this agent's responsibility is
"establish a timing benchmark from text alone" — genuinely different inputs
and failure modes, even though both produce the same shape of output
(DialogueLine.start_time/end_time) for the next stage (VoiceProfilingAgent) to
consume identically either way.

Deliberately generates this benchmark through the REAL Gemini TTS engine
(synthesize_dubbed_audio) rather than a cheap words-per-minute heuristic —
the whole point of a timing benchmark is to predict how the SAME engine that
will produce the real dub later actually paces this text; a generic
heuristic would silently diverge from Gemini's own delivery speed, defeating
the purpose. This is also knowingly accepted as a LESS reliable timing proxy
than a video-first job's real-human-speech benchmark: unlike a real dub
(which a human reviews before publishing, per AGENTS.md principle #5), this
audio is discarded immediately with no review step — if the TTS model ever
misreads/leaks the instruction wrapper (see DUBBING_AUDIO_INSTRUCTION in
prompts.py), the resulting duration is silently off with nothing to catch
it. Accepted trade-off for this version; mux_service.py's atempo clamp still
absorbs the resulting mismatch the same way it already does for a
video-first job's per-line timing errors.
"""

import logging
import wave
from io import BytesIO

from worker.models.pipeline_context import PipelineContext
from worker.services.gemini_client import GeminiClient

logger = logging.getLogger(__name__)

# Voice identity doesn't matter here — this audio is measured for its
# duration only and then discarded, never uploaded/stored/shown to anyone
# (contrast with dubbing_agent.py, where voice_style/voice_name/voice_id
# matter a great deal since that audio IS the final product). Reuses
# DubbingAgent's exact same fallback string for consistency, not because
# this agent cares about "neutral, clear" specifically.
_BENCHMARK_VOICE_STYLE = "neutral, clear"


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """synthesize_dubbed_audio always returns a complete WAV file (header
    already packaged, see gemini_client.py::_pcm_to_wav) — reads it back with
    Python's stdlib wave module rather than shelling out to ffprobe (unlike
    every other duration-probing helper in this codebase, e.g.
    mux_service.py/ingestion_agent.py's _probe_duration-style functions),
    since this is in-memory bytes, not a file on disk, and wave can read a
    WAV header directly from a BytesIO with no temp file needed."""
    with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
        return wav_file.getnframes() / float(wav_file.getframerate())


class ScriptBenchmarkAgent:
    def __init__(self, gemini: GeminiClient):
        self._gemini = gemini

    async def run(self, context: PipelineContext) -> PipelineContext:
        # Deliberately no per-line "already benchmarked, skip it" check here
        # (unlike dubbing_agent.py's `if line.dubbed_audio_gcs_uri: continue`)
        # — a crash mid-run re-benchmarking every line from scratch wastes a
        # little Gemini TTS quota, but translation_agent.py/safety_agent.py
        # already accept this exact same trade-off for their own per-line
        # Gemini calls (they always reprocess every line unconditionally,
        # with no "already translated" skip either); matching that
        # established convention instead of inventing a new one here,
        # especially since a value-based "is this still the original
        # placeholder timestamp" check would be fragile once SOME lines in
        # the same run have already been overwritten with real, unrelated
        # cumulative values.
        cursor_seconds = 0.0
        benchmarked_lines = []
        for line in context.dialogue_lines:
            try:
                audio_bytes = await self._gemini.synthesize_dubbed_audio(
                    # Prefixed so this throwaway call can NEVER share a cache
                    # key with DubbingAgent's real dub of the same
                    # DialogueLine row (found via gemini-prompt-reviewer,
                    # review 2026-10-11): gemini_client.py::_cache_key does
                    # NOT include character_id, and uses the pre-resolution
                    # voice_name (None here, same as a character with no
                    # resolved voice yet) — a same-language, same-text,
                    # same-fallback-style real dub call could otherwise
                    # cache-hit this discarded benchmark audio and silently
                    # ship it as the final deliverable, voiced wrong and
                    # sourced from pre-translation text, with no error
                    # surfaced anywhere.
                    dialogue_line_id=f"benchmark:{line.id}",
                    text=line.original_text,
                    target_lang="en",
                    voice_style=_BENCHMARK_VOICE_STYLE,
                    character_id=None,
                    voice_name=None,
                    voice_id=None,
                )
                duration_seconds = _wav_duration_seconds(audio_bytes)
            except Exception:
                # A genuine system boundary (an external API call) — one
                # rare failure (e.g. exhausted retries inside
                # synthesize_dubbed_audio) must not crash the WHOLE job,
                # mirroring ingestion_agent.py's "skip 1 bad line" pattern.
                # The line's DB row keeps its original placeholder timestamp
                # and is simply excluded from the REST of this pipeline run
                # (never translated/dubbed this time) — a human reviewer can
                # still see it in the review dashboard with no
                # translated_text, same visibility as any other
                # never-processed line.
                logger.warning(
                    "job %s: skipping benchmark for 1 line whose synthesis failed: %r",
                    context.job_id,
                    line.id,
                )
                continue
            line.start_time = cursor_seconds
            line.end_time = cursor_seconds + duration_seconds
            cursor_seconds = line.end_time
            benchmarked_lines.append(line)

        if context.dialogue_lines and not benchmarked_lines:
            # ALL lines failing is more likely a real outage/bug (e.g. the
            # TTS model's daily quota already exhausted) than normal bad
            # data — raise so it surfaces immediately instead of silently
            # producing a job with zero dialogue lines left to review, same
            # reasoning as ingestion_agent.py's identical guard.
            raise RuntimeError(
                f"job {context.job_id}: all {len(context.dialogue_lines)} lines failed "
                "to get a timing benchmark — likely a Gemini outage/quota issue, not "
                "bad script data"
            )

        context.dialogue_lines = benchmarked_lines
        context.current_stage = "transcribed"
        return context
