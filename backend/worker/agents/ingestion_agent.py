"""Agent 1 — Ingestion & Transcription (see docs/agent-pipeline.md).

Input: context.source_video_gcs_uri
Output: context.dialogue_lines (transcript with timestamp + speaker_tag)
"""

import asyncio
import logging
import math
import tempfile
import uuid
from pathlib import Path

import ffmpeg

from worker.models.pipeline_context import DialogueLine, PipelineContext
from worker.services.gemini_client import GeminiClient
from worker.services.storage import StorageClient

logger = logging.getLogger(__name__)

# Maximum gap between 2 consecutive lines for them to still be considered the
# SAME speaker (the transcription only split the line due to a brief pause
# mid-sentence, not an actual change of speaker) — see
# _normalize_speaker_tags. The small value is intentional: it should only
# catch natural pauses while speaking (taking a breath, a sentence break),
# not incorrectly merge two characters replying to each other in quick
# succession (which can also have a similarly short gap).
_SAME_SPEAKER_GAP_THRESHOLD_SECONDS = 0.3


def _normalize_speaker_tags(dialogue_lines: list[DialogueLine]) -> None:
    """Fix the speaker_tag for lines that the transcription mistakenly
    assigned to a different speaker just because of a short pause mid-sentence
    (we got real feedback: "different voice clone even though it's the same
    character in the original sentence" — one sentence got split into
    multiple dialogue_lines because of a pause, and Gemini transcription
    sometimes switches the speaker_tag label right at that split point even
    though it's still one person speaking continuously).

    Consequence if NOT fixed: VoiceProfilingAgent assigns a different
    character_id to each half of the same sentence -> DubbingAgent picks a
    different TTS voice -> it sounds like two different characters speaking
    back-to-back within a single sentence.

    One-directional heuristic, processed sequentially left->right: if the gap
    to the PREVIOUS line (already normalized) is smaller than the threshold,
    reassign the current line's speaker_tag to MATCH the previous line — we
    don't use Gemini here (cheap, no quota cost) because this is a fact about
    time (the gap between two timestamps), it doesn't need semantic
    reasoning.

    A negative gap (two lines with overlapping timestamps, e.g. noisy
    transcription when two people genuinely talk over each other) still gets
    merged because < threshold is always true — we accept this trade-off:
    prioritizing a correct fix for the common case (a sentence split by a
    pause) over adding a guard for a rare edge case with no evidence of
    actually occurring, to avoid making the heuristic more complex without
    data to tune it."""
    for i in range(1, len(dialogue_lines)):
        prev_line, curr_line = dialogue_lines[i - 1], dialogue_lines[i]
        if curr_line.speaker_tag == prev_line.speaker_tag:
            continue
        gap = curr_line.start_time - prev_line.end_time
        if gap < _SAME_SPEAKER_GAP_THRESHOLD_SECONDS:
            curr_line.speaker_tag = prev_line.speaker_tag


def _parse_timestamp_seconds(value: object) -> float:
    """Normalize a timestamp returned by Gemini (transcribe_audio) into a
    plain number of seconds — real bug encountered (reviewed 2026-10-09):
    TRANSCRIPTION_PROMPT clearly asks for "seconds, as a float" but
    _JSON_CONFIG (gemini_client.py) only forces
    response_mime_type=application/json, with NO response_schema to enforce
    the TYPE of each field (one shared config is used across many call sites
    with different JSON shapes, so no per-site schema is attached) — Gemini
    occasionally returns a "mm:ss.mmm"-formatted string (e.g. "00:02.100")
    instead of a plain float, which makes `DialogueLine(start_time=...)`
    raise a ValidationError, crashing the whole job (there's no retry/resume
    mechanism, see worker/pipeline.py).

    This is a genuine system boundary (output from an external API, not
    internally trusted data), so we accept both formats here instead of
    raising hard — supporting "mm:ss(.mmm)" and "hh:mm:ss(.mmm)" in case
    Gemini also decides to use the longer format for longer videos.

    Raises ValueError (so IngestionAgent.run can catch it and skip just the
    bad line, see below) if the result is negative or not a finite number
    (NaN/Inf) — Python's `float()` itself accepts "nan"/"inf"/"-inf"
    (case-insensitive) and negative numbers WITHOUT raising, so without this
    explicit check, a malformed transcript line of this kind would slip
    through silently, producing a DialogueLine with a nonsensical timestamp
    instead of being caught (bug found in review 2026-10-10)."""
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        text = str(value).strip()
        if ":" not in text:
            seconds = float(text)
        else:
            parts = text.split(":")
            seconds = float(parts[-1])
            if len(parts) >= 2:
                seconds += float(parts[-2]) * 60
            if len(parts) >= 3:
                seconds += float(parts[-3]) * 3600
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"invalid timestamp: {value!r} (parsed as {seconds})")
    return seconds


def _probe_local_duration_seconds(path: str) -> float:
    """Blocking (shells out to ffprobe) — always call via asyncio.to_thread,
    see IngestionAgent.run."""
    return float(ffmpeg.probe(path)["format"]["duration"])


def _drop_lines_past_video_duration(
    items: list[dict], video_duration_seconds: float, job_id: str
) -> list[dict]:
    """Deterministic code-level backstop for a real bug found via feedback,
    confirmed on real job data: Gemini (transcribe_audio) assigned a
    dialogue line an end_time of 102.5s for a video that was actually only
    65.8s long — the line's real speech was only ~4.6s, but the model lost
    track of elapsed time and kept the slot going far past where the media
    itself had already ended. Downstream (mux_service.py), this left the
    ORIGINAL audio wrongly ducked/muted for the ~40s gap between where the
    dub clip actually finished and where the (wrong) nominal slot claimed it
    would finish, which is what the reviewer perceived as "the video
    pauses". TRANSCRIPTION_DURATION_CONSTRAINT (prompts.py) asks the model to
    avoid this, but that's a prompt-level mitigation, not a guarantee — this
    check runs unconditionally and catches it even if the model ignores the
    prompt.

    Two different corrections depending on whether ANY part of the line
    still falls within the real video:
    - start_time already >= the real duration: the line's declared position
      is ENTIRELY outside real content, so there's no sensible place left to
      put it — drop it (logged). Its text is lost, but keeping a
      provably-impossible position would just recreate the exact dead-air
      bug this backstop exists to prevent.
    - only end_time exceeds the real duration (start_time is still valid):
      the line's speech genuinely started within the real video, only the
      model's estimate of when it ends drifted past the end — clamp
      end_time down to the real duration instead of dropping the whole line,
      preserving the line (and its eventual dub) for the reviewer.

    Operates on the raw dicts from GeminiClient.transcribe_audio, BEFORE
    DialogueLine construction, so a clamped end_time flows into the existing
    per-line parsing loop in IngestionAgent.run exactly like any other
    value — no separate code path needed there."""
    kept: list[dict] = []
    for item in items:
        try:
            start = _parse_timestamp_seconds(item["start_time"])
        except (KeyError, ValueError, TypeError):
            # Malformed independently of duration — let the existing per-line
            # loop in IngestionAgent.run catch and log this; not this
            # function's concern.
            kept.append(item)
            continue
        if start >= video_duration_seconds:
            logger.warning(
                "job %s: dropping 1 transcript line whose start_time (%.1fs) is "
                "already past the video's real duration (%.1fs): %r",
                job_id,
                start,
                video_duration_seconds,
                item,
            )
            continue
        try:
            end = _parse_timestamp_seconds(item["end_time"])
        except (KeyError, ValueError, TypeError):
            kept.append(item)
            continue
        if end > video_duration_seconds:
            logger.warning(
                "job %s: clamping 1 transcript line's end_time from %.1fs down to "
                "the video's real duration %.1fs: %r",
                job_id,
                end,
                video_duration_seconds,
                item,
            )
            item = {**item, "end_time": video_duration_seconds}
        kept.append(item)
    return kept


class IngestionAgent:
    def __init__(self, gemini: GeminiClient, gcs: StorageClient):
        self._gemini = gemini
        self._gcs = gcs

    async def run(self, context: PipelineContext) -> PipelineContext:
        # GeminiClient.transcribe_audio needs a local file path to upload via
        # the Gemini File API (the Developer API, using an API key, can't
        # read a gs:// URI directly, unlike Vertex AI) — download it to a
        # temp dir first, see worker/services/gemini_client.py:transcribe_audio.
        with tempfile.TemporaryDirectory(prefix=f"ingest-{context.job_id}-") as tmp_dir:
            # Keep the original file's extension (e.g. .mp4) so the Gemini
            # File API can auto-detect the mime_type from the filename — don't
            # use a generic temp name without an extension.
            suffix = Path(context.source_video_gcs_uri).suffix or ".mp4"
            local_path = str(Path(tmp_dir) / f"source{suffix}")
            await self._gcs.download_to_path(context.source_video_gcs_uri, local_path)
            raw_lines = await self._gemini.transcribe_audio(local_path)
            # Code-level backstop (see _drop_lines_past_video_duration) for
            # Gemini hallucinating a timestamp past where the media actually
            # ends — must run here, BEFORE the temp dir (and local_path with
            # it) gets cleaned up on exiting this `with` block.
            try:
                video_duration = await asyncio.to_thread(_probe_local_duration_seconds, local_path)
            except Exception:
                logger.warning(
                    "job %s: could not probe the downloaded video's real duration — "
                    "skipping the out-of-bounds-timestamp backstop for this job",
                    context.job_id,
                )
            else:
                raw_lines = _drop_lines_past_video_duration(
                    raw_lines, video_duration, context.job_id
                )

        dialogue_lines: list[DialogueLine] = []
        for item in raw_lines:
            try:
                dialogue_lines.append(
                    DialogueLine(
                        id=str(uuid.uuid4()),
                        speaker_tag=item["speaker_tag"],
                        start_time=_parse_timestamp_seconds(item["start_time"]),
                        end_time=_parse_timestamp_seconds(item["end_time"]),
                        original_text=item["text"],
                    )
                )
            except (KeyError, ValueError, TypeError):
                # A genuine system boundary (Gemini's JSON output) — even
                # though there's already a response_schema enforcing types
                # (see gemini_client.py::_TranscriptionItem) and
                # _parse_timestamp_seconds handles the 2 common timestamp
                # formats, there's still no absolute 100% guarantee for every
                # line. A single rare malformed line MUST NOT be allowed to
                # crash the WHOLE job (there's no retry/resume mechanism, see
                # worker/pipeline.py) — skip just that line, keep the rest, so
                # the reviewer still gets a usable video instead of the job
                # being stuck forever in "queued".
                logger.warning(
                    "job %s: skipping 1 malformed transcript line: %r",
                    context.job_id,
                    item,
                )
        if raw_lines and not dialogue_lines:
            # ALL lines failing is very different from "one rare malformed
            # line" — it's more likely a real programming bug (e.g. someone
            # added a new required field to DialogueLine but forgot to pass it
            # here) rather than bad data from Gemini, because the
            # `except ValueError` above also catches
            # pydantic.ValidationError (a subclass of ValueError) for ANY
            # validation error, not just timestamps. We must raise to surface
            # the error immediately, and must not silently let the job
            # continue with an empty dialogue_lines (the video reaching the
            # reviewer with no dialogue lines left at all, with no one
            # knowing why — violating the spirit of AGENTS.md's "always have
            # a human review step", since there's nothing left to review).
            raise RuntimeError(
                f"job {context.job_id}: all {len(raw_lines)} transcript lines failed to "
                "parse — likely a code bug, not bad data from Gemini"
            )
        context.dialogue_lines = dialogue_lines
        _normalize_speaker_tags(context.dialogue_lines)
        context.current_stage = "transcribed"
        return context
