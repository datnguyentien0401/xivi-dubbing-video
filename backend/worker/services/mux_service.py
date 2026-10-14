"""Merges the dubbed audio into the original video by timestamp — uses
ffmpeg, NOT an AI agent.

See docs/agent-pipeline.md — this is the step between Agent 5 (Dubbing) and
the Review step, and doesn't need Gemini. `ffmpeg` (synchronous, CPU-heavy)
runs via asyncio.to_thread so it doesn't block the event loop, following the
same convention as gcs_client.py.
"""

import asyncio
import os
import uuid
import wave

import ffmpeg

from worker.models.pipeline_context import PipelineContext
from worker.services.storage import StorageClient

# Minimum floor for a dialogue line's time slot — avoids dividing by zero/a
# negative number if end_time == start_time (a rare transcript data error).
# Public (no "_" prefix) because
# api/routers/jobs.py::_build_default_timeline (specs/002-video-editor) also
# needs this SAME value when computing the default source_end for a dialogue
# clip — the two places must agree since they describe the same concept (the
# slot length that the dubbed audio is time-stretched to fit exactly), and
# any mismatch would make the default snapshot not match the real muxed
# audio's length.
MIN_DIALOGUE_SLOT_SECONDS = 0.1


def _wav_duration_seconds(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / wf.getframerate()


# Clamps the time-stretch ratio to [0.8, 1.25] (max 25% deviation) — enough
# to fine-tune the small residual error left over (TTS never matches exactly
# even though the translation prompt already asked for an approximate length,
# see shared/prompts.py:DIALOGUE_TRANSLATION_PROMPT), but does NOT allow
# strong speed-up/slow-down like before (which used to go up to 4x) — that
# sounded abnormally sped up and hard to understand (real feedback from
# review). Tradeoff: if the translation still deviates a lot in duration
# (rare, thanks to the prompt already asking for an approximate length right
# from the translation step), the dub clip may run slightly past its slot
# instead of being forced to an odd-sounding speed — this tradeoff is
# accepted because intelligibility is prioritized over an exact match, per
# the actual user requirement.
_ATEMPO_MIN_RATIO = 0.8
_ATEMPO_MAX_RATIO = 1.25


def _clamped_atempo_ratio(ratio: float) -> float:
    """The actual speed factor atempo will apply, after clamping to
    [_ATEMPO_MIN_RATIO, _ATEMPO_MAX_RATIO] — split out from
    _decompose_atempo_factors (single source of truth) because callers also
    need this value on its own, not just the decomposed per-application
    factor list (see its use in _encode_audio_track, computing how long a
    dub clip ACTUALLY ends up after stretching, which can differ from the
    nominal slot_duration whenever the ideal ratio needed clamping)."""
    return max(_ATEMPO_MIN_RATIO, min(_ATEMPO_MAX_RATIO, ratio))


def _decompose_atempo_factors(ratio: float) -> list[float]:
    """atempo only accepts a factor in [0.5, 2.0] per application — a ratio
    outside that range must be split into multiple consecutive applications
    (e.g. 0.3 = apply 0.5 then apply 0.6, since 0.5 * 0.6 = 0.3). The overall
    ratio is clamped to [_ATEMPO_MIN_RATIO, _ATEMPO_MAX_RATIO] first — with
    this narrow clamp range, in practice exactly one application is always
    enough (the loop below is kept for generality, independent of the
    specific constant values). Returns a list of factors that multiply back
    to the clamped ratio."""
    remaining = _clamped_atempo_ratio(ratio)
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return factors


def _atempo_chain(stream, ratio: float):
    for factor in _decompose_atempo_factors(ratio):
        stream = stream.filter("atempo", factor)
    return stream


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _write_srt(lines: list[tuple[float, float, str]], path: str) -> None:
    """Generates TRANSLATED subtitles to burn into the video, REPLACING the
    original subtitles (if the original video has a separable subtitle track,
    the output here does not keep that track — the new track generated from
    translated_text is the only subtitle track that remains)."""
    with open(path, "w", encoding="utf-8") as f:
        for index, (start, end, text) in enumerate(lines, start=1):
            f.write(f"{index}\n")
            f.write(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n")
            f.write(f"{text}\n\n")


def _has_audio_stream(path: str) -> bool:
    """A source video sometimes has no audio track at all (rare but possible)
    — must check before `ffmpeg.input(path).audio`, since that filter itself
    doesn't raise right away, only failing once `.run()` actually executes,
    with a much more confusing error than skipping early here."""
    probe = ffmpeg.probe(path)
    return any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))


def _duck_original_audio_during_dialogue(
    original_audio_stream, dialogue_slots: list[tuple[float, float]]
):
    """Fully mutes the ORIGINAL audio track exactly within each dubbed
    dialogue line's time slot (so the original voice doesn't play over the
    dubbed voice at the same time) — OUTSIDE those slots, the original audio
    is kept 100% intact. This is the fix for a real bug encountered (review
    feedback 2026-10-10): previously the output audio track was built
    ENTIRELY from the dub clips, never referencing the original audio at
    all — every sound effect/background music/ambient sound (anything that
    isn't a character's dialogue) was lost completely throughout the whole
    video, not just while someone was speaking.

    Known limitation: if a sound effect/background music happens to OVERLAP
    exactly with a character speaking (the same time slot as a dialogue
    line), that portion is still lost along with the original voice in that
    slot — separating speech from background music/sound effects (audio
    source separation) is a completely different problem, out of scope for
    this fix (see AGENTS.md "Simple first, optimize later")."""
    stream = original_audio_stream
    for start_time, end_time in dialogue_slots:
        stream = stream.filter(
            "volume", volume=0, enable=f"between(t,{start_time:.3f},{end_time:.3f})"
        )
    return stream


def _encode_audio_track(
    source_video_path: str,
    dubbed_audio_clips: list[tuple[float, float, str]],
    output_audio_path: str,
) -> None:
    """Pass 1/3: processes audio only — time-stretches each clip to fit its
    slot, mixes it at the correct time position TOGETHER WITH the original
    audio (already muted within dialogue slots, see
    _duck_original_audio_during_dialogue), normalizes the format, and encodes
    AAC. Kept separate from video encoding (see _encode_video_track) and the
    final mux step (see _mux_streams) — previously everything was combined
    into ONE ffmpeg command (complex audio filtering, subtitle-burning video
    filtering, and encoding both at once), and that complex filtergraph was
    suspected to be the actual cause of the timing/packaging bug that made
    Chrome refuse to play it (AUDIO_RENDERER_ERROR), even though each
    individual aspect (sample rate, channel layout, AAC profile, container
    structure, source WAV...) had been verified clean firsthand — splitting
    into 3 separate passes keeps each step simple, independently testable,
    and rules out any interaction between two filter chains in one command.

    Each dubbed clip is time-stretched (atempo, pitch preserved) to exactly
    fit the time slot (end_time - start_time) of the original dialogue line
    BEFORE delay/mix — otherwise the translated line (usually longer/shorter
    than the original) could spill into the next line's slot, with the two
    audio tracks overlapping and sounding garbled (reproduced this firsthand
    during review: "audio sounds overlapping")."""
    delayed_streams = []
    # The ACTUAL duration each dub clip occupies after stretching — tracked
    # per clip because it's NOT always slot_duration (see below).
    actual_durations: list[float] = []
    for start_time, end_time, audio_path in dubbed_audio_clips:
        slot_duration = max(MIN_DIALOGUE_SLOT_SECONDS, end_time - start_time)
        clip_duration = _wav_duration_seconds(audio_path)
        ratio = clip_duration / slot_duration
        # Real bug found via feedback ("the audio right before is stretched
        # out even though it already ran out" — a ~40s silent gap before the
        # next line): a dialogue line's transcribed slot can be WAY longer
        # than its actual speech (e.g. the source transcript mis-measured a
        # ~4.6s line's slot as ~44s) — stretching to fill a slot that
        # extreme would need ratio ~0.1, far past the [_ATEMPO_MIN_RATIO,
        # _ATEMPO_MAX_RATIO] clamp (intentionally narrow — see its own
        # comment), so the clip only stretches to clip_duration /
        # _clamped_atempo_ratio(ratio), MUCH shorter than slot_duration. The
        # dub-clip stream itself already behaves correctly either way (it
        # only ever plays its own real length) — the bug was that the
        # ORIGINAL audio's duck window (below) used to always span the full
        # nominal slot_duration regardless, muting real ambient
        # sound/original audio for the entire mismeasured slot even long
        # after the dub clip had already finished playing.
        actual_durations.append(clip_duration / _clamped_atempo_ratio(ratio))

        stream = ffmpeg.input(audio_path)
        stream = _atempo_chain(stream, ratio)
        delay_ms = max(0, round(start_time * 1000))
        stream = stream.filter("adelay", f"{delay_ms}|{delay_ms}")
        delayed_streams.append(stream)

    streams_to_mix = list(delayed_streams)
    if _has_audio_stream(source_video_path):
        original_audio = ffmpeg.input(source_video_path).audio
        # Mute window = the dub clip's REAL post-stretch duration (see
        # actual_durations above), not the nominal slot_duration — they're
        # identical in the normal case (slot_duration IS exactly what the
        # ideal, unclamped ratio would stretch the clip to), so this changes
        # nothing when the ratio didn't need clamping; it only matters (and
        # is the actual fix) when the ideal ratio got clamped, where the
        # dub clip genuinely ends well before the slot's nominal end.
        dialogue_slots = [
            (start, start + max(MIN_DIALOGUE_SLOT_SECONDS, duration))
            for (start, _end, _path), duration in zip(
                dubbed_audio_clips, actual_durations, strict=True
            )
        ]
        original_audio = _duck_original_audio_during_dialogue(original_audio, dialogue_slots)
        streams_to_mix.insert(0, original_audio)

    # `original_audio` (decoded directly from the source container, whose
    # sample_rate/channel layout can be completely different from the mono
    # 24kHz TTS WAV) does NOT need its own aformat before amix — ffmpeg
    # auto-negotiates the format between amix's inputs; only a SINGLE
    # normalization pass is needed, at the `aformat` right below, applied to
    # the already-mixed result, before encoding.
    mixed_audio = ffmpeg.filter(
        streams_to_mix, "amix", inputs=len(streams_to_mix), dropout_transition=0, normalize=0
    )
    # aformat: normalizes sample_rate/channel layout RIGHT BEFORE encoding,
    # regardless of whatever intermediate format the atempo/amix filter chain
    # above produced — 44.1kHz/stereo instead of inheriting the mono 24kHz
    # straight from Gemini TTS (_AUDIO_SAMPLE_RATE_HZ, a format rarely seen in
    # real-world media).
    mixed_audio = mixed_audio.filter("aformat", sample_rates=44100, channel_layouts="stereo")
    # asetnsamples: AAC-LC encodes in fixed frames of 1024 samples (44.1kHz =>
    # 0.023220s/frame — matching exactly the duration observed on every
    # packet via ffprobe). Without this filter, the LAST frame of the stream
    # (the remainder that doesn't evenly divide into 1024 samples after the
    # whole atempo/adelay/amix chain above) gets flushed by the raw AAC
    # encoder as a malformed/extremely short packet (caught firsthand: the
    # last packet was only 6 bytes, duration 0.000363s — compared to
    # ~350+ bytes/0.023220s for every other packet) — Chrome
    # (FFmpegAudioDecoder, unable to pick a hardware decoder for AAC) rejects
    # this packet and fires PipelineStatus::AUDIO_RENDERER_ERROR right at the
    # point of playback near the end of the audio track (see a real
    # chrome://media-internals dump from the user — the error happens after
    # already reaching BUFFERING_HAVE_ENOUGH and while kPlaying, not a
    # stream-opening error). asetnsamples (pad=1 by default) regroups the
    # audio into frames of exactly 1024 samples, zero-padding the last
    # incomplete frame up to 1024 samples instead of leaving a stray
    # fragment.
    mixed_audio = mixed_audio.filter("asetnsamples", n=1024, p=1)

    (
        ffmpeg.output(
            mixed_audio,
            output_audio_path,
            acodec="aac",
            audio_bitrate="128k",
            # aac_low = AAC-LC, the most widely compatible profile.
            **{"profile:a": "aac_low"},
        )
        .overwrite_output()
        .run(quiet=True)
    )


def _encode_video_track(source_video_path: str, srt_path: str, output_video_path: str) -> None:
    """Pass 2/3: processes video only — burns in the translated subtitles
    (replacing the original ones if any), re-encodes (stream-copy is no
    longer possible since a filter is applied directly to the frames).
    Does NOT touch audio at this step (`-an`)."""
    video_stream = ffmpeg.input(source_video_path).video.filter("subtitles", srt_path)
    (
        ffmpeg.output(video_stream, output_video_path, vcodec="libx264", an=None)
        .overwrite_output()
        .run(quiet=True)
    )


def _probe_duration(path: str) -> float:
    return float(ffmpeg.probe(path)["format"]["duration"])


def _pad_video_to_duration(video_path: str, target_duration: float, output_path: str) -> None:
    """Extends `video_path` by freezing its LAST FRAME until it reaches
    `target_duration` — needed when the dubbed audio track ends up LONGER
    than the source video's own real length (see mux_dubbed_audio).

    Real bug found via feedback: "the video has a ~40s pause right before
    the final line" — root-caused on real job data to a dialogue line's
    transcribed end_time (102.5s) landing far past the source video's own
    real duration (65.8s), because a later line's start_time (103s) was ALSO
    past it. Without this fix, the final muxed container ended up with a
    VIDEO track (65.8s) shorter than its AUDIO track (106.2s, built around
    the dialogue timestamps) — confirmed directly via ffprobe on the actual
    broken file. A container with mismatched track durations is effectively
    malformed: most players either stop playback the moment the video stream
    ends (never reaching the last line's audio at all) or freeze on the last
    frame while the audio keeps playing underneath — which is exactly what
    looked like "the video pauses" (it wasn't paused, the video stream had
    simply run out while the audio stream hadn't).

    Deliberately does NOT trim the audio down to the video's length instead
    (the opposite fix) — that would SILENTLY DROP the last line's dubbed
    audio entirely, which is worse than a frozen-frame stretch: the
    underlying transcription timestamp bug is a Gemini accuracy issue this
    code can't deterministically correct (see
    worker/agents/ingestion_agent.py::_parse_timestamp_seconds for a
    different, format-only class of timestamp bug already fixed there — this
    one is a valid number that's just the wrong VALUE, which no parsing fix
    can catch), so padding to keep all dubbed content audible — with a
    visible frozen frame instead of an unpredictable broken player state —
    is the safer default until the root transcription accuracy issue is
    addressed. `tpad` with stop_mode="clone" repeats the actual last decoded
    frame, not a black/blank filler."""
    extra = target_duration - _probe_duration(video_path)
    if extra <= 0:
        os.replace(video_path, output_path)
        return
    (
        ffmpeg.input(video_path)
        .filter("tpad", stop_mode="clone", stop_duration=extra)
        .output(output_path, vcodec="libx264", an=None)
        .overwrite_output()
        .run(quiet=True)
    )


def _mux_streams(video_path: str, audio_path: str, output_path: str) -> None:
    """Pass 3/3: merges the 2 already-encoded streams (video from pass 2 +
    audio from pass 1) into one container — ONLY remuxes (`-c copy`, no
    filtering/re-encoding at all) so this final step is as simple/safe as
    possible, with no opportunity to introduce a new timing/packaging bug."""
    video_input = ffmpeg.input(video_path)
    audio_input = ffmpeg.input(audio_path)
    (
        ffmpeg.output(
            video_input.video,
            audio_input.audio,
            output_path,
            vcodec="copy",
            acodec="copy",
            # movflags=faststart: by default ffmpeg writes the "moov" atom
            # (metadata the browser needs to read first to know the file's
            # structure/duration) at the END of the file — an HTML5 <video>
            # playing over a URL (streaming/progressive, see
            # ReviewDashboard.tsx) can't read it, and it looks "stuck"/unable
            # to play even though the file itself isn't broken (reproduced
            # firsthand: ffprobe confirms the file is 100% valid, but the
            # moov atom sits at the very last offset of the file). Add this
            # flag so ffmpeg writes moov at the START of the file.
            movflags="faststart",
        )
        .overwrite_output()
        .run(quiet=True)
    )


def mux_dubbed_audio(
    source_video_path: str,
    dubbed_audio_clips: list[tuple[float, float, str]],  # [(start_time, end_time, audio_path), ...]
    subtitle_lines: list[
        tuple[float, float, str]
    ],  # [(start_time, end_time, translated_text), ...]
    output_path: str,
) -> str:
    """Merges the dubbed audio clips into the correct time positions on the
    original video, mixes them TOGETHER WITH the original audio (sound
    effects/background music/ambient sound are kept intact, only the
    original audio is muted exactly within slots that have dubbed dialogue —
    see _duck_original_audio_during_dialogue), and burns in the translated
    subtitles in place of the original ones — split into 3 separate passes
    (audio, video, final mux), see each helper function's docstring for why.

    Known limitation: a sound effect/background music that OVERLAPS exactly
    with dialogue is still lost along with the original voice in that slot
    (see _duck_original_audio_during_dialogue) — separating speech from
    background music is a different problem, out of scope for this fix.
    """
    if not dubbed_audio_clips:
        raise ValueError("No audio clips to mux")

    srt_path = output_path + ".srt"
    _write_srt(subtitle_lines, srt_path)

    audio_path = output_path + ".audio.m4a"
    _encode_audio_track(source_video_path, dubbed_audio_clips, audio_path)

    video_path = output_path + ".video.mp4"
    _encode_video_track(source_video_path, srt_path, video_path)

    # Keep the video track at LEAST as long as the audio track — see
    # _pad_video_to_duration for the real bug this prevents (a shorter video
    # track than audio track makes the final container effectively
    # malformed). A no-op (just renames the file) in the normal case where
    # the audio naturally ends within the video's own length.
    padded_video_path = output_path + ".video-padded.mp4"
    _pad_video_to_duration(video_path, _probe_duration(audio_path), padded_video_path)

    _mux_streams(padded_video_path, audio_path, output_path)
    return output_path


async def mux_for_job(
    context: PipelineContext, gcs_client: StorageClient, local_tmp_dir: str
) -> str:
    """High-level function called by pipeline.py — downloads assets, calls
    mux_dubbed_audio, uploads the result.

    Only muxes lines that were successfully dubbed (safety_status == "ok"
    and has a dubbed_audio_gcs_uri) — flagged/undubbed lines keep the
    original audio silenced at that spot, waiting for a reviewer to handle
    them (doesn't block the whole job). The subtitles burned into the video,
    however, use ALL lines that have translated_text (including lines that
    don't/couldn't get audio dubbed) — losing audio doesn't mean losing the
    translated subtitles too.
    """
    os.makedirs(local_tmp_dir, exist_ok=True)
    source_video_path = os.path.join(local_tmp_dir, "source.mp4")
    await gcs_client.download_to_path(context.source_video_gcs_uri, source_video_path)

    dubbed_clips: list[tuple[float, float, str]] = []
    for line in context.dialogue_lines:
        if not line.dubbed_audio_gcs_uri:
            continue
        local_audio_path = os.path.join(local_tmp_dir, f"{line.id}.wav")
        await gcs_client.download_to_path(line.dubbed_audio_gcs_uri, local_audio_path)
        dubbed_clips.append((line.start_time, line.end_time, local_audio_path))

    if not dubbed_clips:
        raise RuntimeError(f"job {context.job_id}: no successfully dubbed lines to mux")

    subtitle_lines = [
        (line.start_time, line.end_time, line.translated_text)
        for line in context.dialogue_lines
        if line.translated_text
    ]

    output_path = os.path.join(local_tmp_dir, f"muxed-{uuid.uuid4()}.mp4")
    await asyncio.to_thread(
        mux_dubbed_audio, source_video_path, dubbed_clips, subtitle_lines, output_path
    )

    # uuid4 in the filename — does NOT reuse the exact same path on every mux
    # (same reasoning as dubbing_agent.py: redubbing a line and re-muxing
    # would overwrite the EXACT same old path jobs/{job_id}/muxed/{lang}.mp4;
    # a browser that already cached that URL from a previous view would keep
    # playing the OLD video even though the backend has correctly muxed a new
    # one, even after adding Cache-Control: no-store — the new header can't
    # "unpoison" a cache the browser already saved BEFOREHAND. Changing the
    # path on every mux eliminates entirely any chance of hitting the old
    # cache).
    blob_name = f"jobs/{context.job_id}/muxed/{context.target_language}-{uuid.uuid4().hex[:8]}.mp4"
    return await gcs_client.upload_from_path(output_path, blob_name)
