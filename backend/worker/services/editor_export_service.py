"""Renders the export from `jobs.timeline_edits` (Video Editor, specs/002-video-editor)
— uses ffmpeg, NOT an AI agent (same role/convention as mux_service.py: runs
via asyncio.to_thread so it doesn't block the event loop, builds the filter chain
with ffmpeg-python instead of hand-assembling command strings).

Core convention: `Clip.timeline_start` (see api/schemas/schemas.py) is the
REAL position on the final OUTPUT timeline, THE SAME for every track type
(video/audio_original/dialogue) — there is no separate "ripple" concept at the
render layer. Ripple or leaving-a-gap are both just ways the editor (frontend,
not yet written) updates a track's `timeline_start` when the user cuts/deletes;
the renderer here only faithfully redraws what `timeline_start` describes — a
gap between 2 clips on the video track -> insert a black frame; a gap on an
audio track -> silence (no extra handling needed, there's simply no clip
there).

MVP scope (see Technical Risks in specs/002-video-editor/plan.md): only
supports cutting/deleting/reordering segments (Video track) and
volume/mute/solo/fade (audio track) — does NOT support time-stretch
(atempo)/changing playback speed the way mux_service.py._atempo_chain does
for the initial dubbing step; stretching a clip here means changing
`source_start`/`source_end` (trimming the source segment shorter/longer), not
changing its playback speed.
"""

import asyncio
import logging
import os
import tempfile
import uuid

import ffmpeg

from api.db.models import DialogueLine, Job
from api.schemas.schemas import Clip, TimelineState, Track
from worker.services.storage import StorageClient

logger = logging.getLogger(__name__)

_FILLER_COLOR = "black"
# Time comparison tolerance (seconds) — avoids inserting a filler/segment that
# is extremely short (near zero) just because of floating-point error
# accumulated across many consecutive clips.
_EPSILON_SECONDS = 0.02


def _probe_video_dimensions(path: str) -> tuple[int, int, str]:
    """Returns (width, height, fps as a "num/den" string) of the source
    video — the filler segment (black frame) MUST have the same
    dimensions/frame rate as clips cut from the source, otherwise the final
    concat step (-c copy, see _build_silent_video) will fail due to mismatched
    codec params between segments. Does NOT handle rotate/display-matrix side
    data (e.g. vertical video from a phone) — acceptable for animated
    content, which rarely carries frame-rotation side data."""
    probe = ffmpeg.probe(path)
    video_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    return (
        int(video_stream["width"]),
        int(video_stream["height"]),
        video_stream.get("r_frame_rate", "25/1"),
    )


def _probe_duration(path: str) -> float:
    """The REAL duration (seconds) of an already-encoded file — used to
    accumulate `cursor` in _build_silent_video instead of trusting the
    DECLARED figure (`clip.source_end - clip.source_start`). These 2 values
    can differ by a few hundredths of a second because
    `_make_trimmed_video_segment` forces a constant frame rate (`-r fps`)
    during re-encoding — rounding to a whole number of frames, which doesn't
    exactly match the declared number of seconds. If `cursor` were
    accumulated from the declared figures instead of the real ones, this
    small error would accumulate across many clips and could make the video
    track end a few frames earlier/later than the audio track — audio is
    always forced to EXACTLY `duration_seconds` via apad+atrim (see
    _build_mixed_audio), but video previously was only TRIMMED if longer
    (`-t duration_seconds`), never "padded" if shorter — a SILENT audio/video
    sync bug, not a crash. Re-measure for real after each segment so cursor
    always reflects reality."""
    probe = ffmpeg.probe(path)
    return float(probe["format"]["duration"])


def _make_filler_segment(
    width: int, height: int, fps: str, duration: float, output_path: str
) -> None:
    (
        ffmpeg.input(f"color=c={_FILLER_COLOR}:s={width}x{height}:r={fps}:d={duration}", f="lavfi")
        .output(output_path, vcodec="libx264", pix_fmt="yuv420p")
        .overwrite_output()
        .run(quiet=True)
    )


def _make_trimmed_video_segment(
    source_path: str, start: float, end: float, fps: str, output_path: str
) -> None:
    (
        ffmpeg.input(source_path, ss=start, to=end)
        .output(output_path, vcodec="libx264", an=None, r=fps, pix_fmt="yuv420p")
        .overwrite_output()
        .run(quiet=True)
    )


def _find_track(timeline: TimelineState, track_type: str) -> Track | None:
    return next((t for t in timeline.tracks if t.type == track_type), None)


def _build_silent_video(timeline: TimelineState, video_source_path: str, tmp_dir: str) -> str:
    """Builds a silent video (no audio, `-an`) at the exact `timeline_start`
    positions of clips on the "video" track — inserts filler (black frame)
    into any gap between 2 clips or at the start/end of the timeline (see the
    convention in the module docstring). Audio is handled COMPLETELY
    separately (see _build_mixed_audio) and muxed back in at the end — same
    "separate pass for video/audio" philosophy as mux_service.py."""
    video_track = _find_track(timeline, "video")
    if video_track is None or not video_track.clips:
        raise ValueError("Timeline has no clips on the video track; cannot export")

    width, height, fps = _probe_video_dimensions(video_source_path)
    ordered_clips = sorted(video_track.clips, key=lambda c: c.timeline_start)

    segment_paths: list[str] = []
    # cursor is always the REAL total duration (re-measured via
    # _probe_duration) of the segments created so far — NOT accumulated from
    # the declared figures on Clip, see the _probe_duration docstring for why.
    cursor = 0.0

    def _add_filler(gap_duration: float) -> None:
        nonlocal cursor
        path = os.path.join(tmp_dir, f"filler-{uuid.uuid4().hex[:8]}.mp4")
        _make_filler_segment(width, height, fps, gap_duration, path)
        segment_paths.append(path)
        cursor += _probe_duration(path)

    for clip in ordered_clips:
        if clip.timeline_start > cursor + _EPSILON_SECONDS:
            _add_filler(clip.timeline_start - cursor)
        path = os.path.join(tmp_dir, f"clip-{uuid.uuid4().hex[:8]}.mp4")
        _make_trimmed_video_segment(
            video_source_path, clip.source_start, clip.source_end, fps, path
        )
        segment_paths.append(path)
        cursor += _probe_duration(path)

    if timeline.duration_seconds > cursor + _EPSILON_SECONDS:
        _add_filler(timeline.duration_seconds - cursor)

    list_path = os.path.join(tmp_dir, "concat-list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in segment_paths:
            f.write(f"file '{path}'\n")

    output_path = os.path.join(tmp_dir, "silent-video.mp4")
    # The format=concat demuxer requires every segment to have the SAME codec
    # params — every segment above (both filler and clips cut from the
    # source) is encoded with the same width/height/fps/pix_fmt so `-c copy`
    # (no re-encoding) is safe.
    # `-t duration_seconds` is now only a safety net that TRIMS if the real
    # total is still slightly larger due to tiny floating-point error (under
    # _EPSILON_SECONDS) — the "pad if shorter" case has already been fixed AT
    # THE ROOT by the real measured cursor above (see _probe_duration), no
    # longer relying on `-t` to pad like before.
    (
        ffmpeg.input(list_path, f="concat", safe=0)
        .output(output_path, c="copy", t=timeline.duration_seconds)
        .overwrite_output()
        .run(quiet=True)
    )
    return output_path


def _resolve_clip_audio_source(
    clip: Clip,
    track: Track,
    video_source_path: str,
    dialogue_audio_paths: dict[uuid.UUID, str],
) -> str | None:
    """The source asset for an audio clip — returns None if the dialogue
    clip has no audio yet (line not dubbed yet / errored out), letting the
    caller skip that clip instead of raising (keeping silence at that
    position, same philosophy as mux_service.py::mux_for_job when it
    encounters a line that couldn't be dubbed)."""
    if track.type == "dialogue":
        if clip.dialogue_line_id is None:
            return None
        return dialogue_audio_paths.get(clip.dialogue_line_id)
    # track "audio_original" — the original audio OF THE SOURCE video itself,
    # not a separate asset (see the track->asset mapping convention in
    # api/routers/jobs.py::_build_default_timeline).
    return video_source_path


def _build_clip_audio_stream(source_path: str, clip: Clip, track_volume: float):
    """One filter branch for exactly one clip: trims by
    source_start/source_end, applies volume (track * clip), fade in/out, then
    delays to the exact timeline_start — does NOT apply atempo/time-stretch
    (see the MVP scope in the module docstring)."""
    duration = clip.source_end - clip.source_start
    stream = ffmpeg.input(source_path, ss=clip.source_start, to=clip.source_end).audio

    combined_volume = track_volume * clip.volume
    if combined_volume != 1.0:
        stream = stream.filter("volume", combined_volume)

    if clip.fade_in_seconds > 0:
        fade_in = min(clip.fade_in_seconds, duration)
        stream = stream.filter("afade", type="in", start_time=0, duration=fade_in)
    if clip.fade_out_seconds > 0:
        fade_out = min(clip.fade_out_seconds, duration)
        stream = stream.filter(
            "afade", type="out", start_time=max(duration - fade_out, 0), duration=fade_out
        )

    delay_ms = max(0, round(clip.timeline_start * 1000))
    # all=1: applies the same delay to EVERY channel regardless of
    # mono/stereo — unlike mux_service.py (which always knows the source is
    # mono 24kHz from Gemini TTS) this can't hardcode a 2-channel
    # "delay|delay" style, because audio_original comes from the source video
    # and may be stereo.
    stream = stream.filter("adelay", delays=str(delay_ms), all=1)
    return stream


def _select_audible_tracks(audio_tracks: list[Track]) -> list[Track]:
    """Mute/solo logic AT THE TRACK LEVEL (clips have no mute/solo field of
    their own): if at least 1 track is soloed, ONLY soloed (and non-muted)
    tracks count toward the mix; a track that is soloed but ALSO muted is
    still excluded (mute wins over solo, standard DAW behavior). Split out
    into a pure function (no ffmpeg) so this logic can be tested
    independently, without running real ffmpeg."""
    any_solo = any(t.solo for t in audio_tracks)
    return [t for t in audio_tracks if not t.muted and (not any_solo or t.solo)]


def _build_mixed_audio(
    timeline: TimelineState,
    video_source_path: str,
    dialogue_audio_paths: dict[uuid.UUID, str],
    tmp_dir: str,
) -> str:
    """Mixes every audio track (audio_original + each dialogue track) into a
    single AAC file, matching the timeline's `duration_seconds` exactly —
    see _select_audible_tracks for the mute/solo rules."""
    audio_tracks = [t for t in timeline.tracks if t.type != "video"]
    audible_tracks = _select_audible_tracks(audio_tracks)

    streams = []
    for track in audible_tracks:
        for clip in track.clips:
            source_path = _resolve_clip_audio_source(
                clip, track, video_source_path, dialogue_audio_paths
            )
            if source_path is None:
                logger.warning(
                    "export: skipping clip %s (track %s) — no source audio yet (dialogue "
                    "line not dubbed yet)",
                    clip.id,
                    track.id,
                )
                continue
            streams.append(_build_clip_audio_stream(source_path, clip, track.volume))

    output_path = os.path.join(tmp_dir, "mixed-audio.m4a")
    if not streams:
        # No valid audio clips at all (everything muted / all tracks empty)
        # — must still output duration_seconds worth of silence, so
        # _mux_video_audio always has a valid audio track to mux in (don't
        # let the exported video end up with no audio track at all).
        (
            ffmpeg.input(f"anullsrc=r=44100:cl=stereo:d={timeline.duration_seconds}", f="lavfi")
            .output(output_path, acodec="aac", audio_bitrate="128k", **{"profile:a": "aac_low"})
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path

    mixed = ffmpeg.filter(streams, "amix", inputs=len(streams), dropout_transition=0, normalize=0)
    # aformat/asetnsamples: same reason as mux_service.py::_encode_audio_track
    # (normalize the format before encoding, to avoid a malformed final AAC
    # packet that makes Chrome refuse to play it — see the original comment
    # there).
    mixed = mixed.filter("aformat", sample_rates=44100, channel_layouts="stereo")
    # apad then atrim forces EXACTLY duration_seconds regardless of whether
    # the mix is shorter (apad pads in extra silence) or longer (atrim trims
    # it) due to rounding/accumulated delay.
    mixed = mixed.filter("apad")
    mixed = mixed.filter("atrim", end=timeline.duration_seconds)
    mixed = mixed.filter("asetpts", "PTS-STARTPTS")
    mixed = mixed.filter("asetnsamples", n=1024, p=1)
    (
        ffmpeg.output(
            mixed, output_path, acodec="aac", audio_bitrate="128k", **{"profile:a": "aac_low"}
        )
        .overwrite_output()
        .run(quiet=True)
    )
    return output_path


def _mux_video_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """Muxes the silent video + mixed audio into a single container — a pure
    remux (`-c copy`), same reason/pattern as mux_service.py::_mux_streams
    (simple, maximally safe, no further filtering/re-encoding at this final
    step). `movflags=faststart` so it can be played back progressively over a
    URL right away (see the original comment in mux_service.py — exact same
    reason)."""
    video_input = ffmpeg.input(video_path)
    audio_input = ffmpeg.input(audio_path)
    (
        ffmpeg.output(
            video_input.video,
            audio_input.audio,
            output_path,
            vcodec="copy",
            acodec="copy",
            movflags="faststart",
        )
        .overwrite_output()
        .run(quiet=True)
    )


def render_export(
    timeline: TimelineState,
    video_source_path: str,
    dialogue_audio_paths: dict[uuid.UUID, str],
    output_path: str,
) -> str:
    """A pure ffmpeg function (synchronous, run via asyncio.to_thread in
    export_for_job) — builds video + audio separately then muxes them,
    following the already-validated timeline exactly. `dialogue_audio_paths`:
    dialogue_line_id -> local path of the dubbed audio file (only needs to
    contain lines that are ACTUALLY referenced by a clip in the timeline —
    see export_for_job)."""
    with tempfile.TemporaryDirectory(prefix="editor-export-") as tmp_dir:
        silent_video_path = _build_silent_video(timeline, video_source_path, tmp_dir)
        audio_path = _build_mixed_audio(timeline, video_source_path, dialogue_audio_paths, tmp_dir)
        _mux_video_audio(silent_video_path, audio_path, output_path)
    return output_path


async def export_for_job(
    job: Job,
    timeline: TimelineState,
    dialogue_lines: list[DialogueLine],
    source_video_gcs_uri: str,
    gcs_client: StorageClient,
    local_tmp_dir: str,
) -> str:
    """A high-level (asynchronous) function — downloads the needed assets,
    calls render_export via asyncio.to_thread, uploads the result — same role
    as mux_service.py::mux_for_job. Only downloads audio for dialogue lines
    that are ACTUALLY referenced by some clip in the timeline (not the whole
    job), and only lines that already have a `dubbed_audio_gcs_uri` (lines
    not dubbed yet are automatically skipped by _build_mixed_audio, which
    keeps silence at that position)."""
    os.makedirs(local_tmp_dir, exist_ok=True)

    source_video_path = os.path.join(local_tmp_dir, "source.mp4")
    await gcs_client.download_to_path(source_video_gcs_uri, source_video_path)

    referenced_line_ids = {
        clip.dialogue_line_id
        for track in timeline.tracks
        for clip in track.clips
        if clip.dialogue_line_id is not None
    }

    dialogue_audio_paths: dict[uuid.UUID, str] = {}
    for line in dialogue_lines:
        if line.id not in referenced_line_ids or not line.dubbed_audio_gcs_uri:
            continue
        local_path = os.path.join(local_tmp_dir, f"dialogue-{line.id}.wav")
        await gcs_client.download_to_path(line.dubbed_audio_gcs_uri, local_path)
        dialogue_audio_paths[line.id] = local_path

    output_path = os.path.join(local_tmp_dir, f"export-{uuid.uuid4()}.mp4")
    await asyncio.to_thread(
        render_export, timeline, source_video_path, dialogue_audio_paths, output_path
    )

    # uuid4 in the filename — same reason as mux_service.py::mux_for_job
    # (avoids the browser reusing a cached URL from a previous export).
    blob_name = f"jobs/{job.id}/exported/{job.target_language}-{uuid.uuid4().hex[:8]}.mp4"
    return await gcs_client.upload_from_path(output_path, blob_name)
