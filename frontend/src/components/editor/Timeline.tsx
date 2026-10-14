import { useMemo, useRef } from "react";
import type { MouseEvent as ReactMouseEvent, UIEvent } from "react";
import clsx from "clsx";
import { Pause, Play, ZoomIn, ZoomOut } from "lucide-react";
import type { Character } from "@/hooks/useCharacters";
import type { DialogueLine } from "@/hooks/useDialogueLines";
import { useEditorStore, type Track } from "@/stores/editorStore";
import { Playhead } from "@/components/editor/Playhead";
import { TrackRow, TRACK_HEIGHT_PX } from "@/components/editor/TrackRow";

const ZOOM_STEP_PX_PER_SECOND = 20;

const TRACK_TYPE_LABELS: Record<Exclude<Track["type"], "dialogue">, string> = {
  video: "Video",
  audio_original: "Original Audio",
};

interface TimelineProps {
  isPlaying: boolean;
  onPlayPause: () => void;
  onSeek: (seconds: number) => void;
  characters: Character[];
  dialogueLines: DialogueLine[];
}

/** Timeline editor (US1 + US3, specs/002-video-editor) — Video/original
 * audio/per-character dialogue tracks, play/pause, drag/click the playhead,
 * zoom in/out, select clip + drag/trim dialogue clip edges (see
 * ClipBlock.tsx), per-track mute/solo/volume (left-hand label column,
 * below). Video split/trim (US2) is a separate task, not yet done. Reads
 * `timeline` directly from editorStore (not passed via props) — this is the
 * SINGLE source of data for every child component, see editorStore.ts.
 *
 * Layout: the "video" track (always exactly one, see
 * jobs.py::_build_default_timeline) is pinned at the top and never scrolls
 * vertically — only the tracks below it (original audio + one "dialogue"
 * track per character) get their own vertical scrollbar, so a video with
 * many characters doesn't push the video track out of view (real UX
 * complaint — a video with 6+ characters made the video track scroll away
 * with the rest). Both the pinned video row and the scrollable audio rows
 * still share ONE horizontal-scroll container (for zoom) — stacked inside
 * it vertically — so there's no second scrollbar to keep in sync.
 *
 * The audio label column (left) and audio track rows (right) are two
 * SEPARATE DOM subtrees — the label column sits outside the horizontal-zoom
 * container entirely, so they can't share one native scroll container the
 * way the pre-split code did (that would also force the label column to
 * scroll horizontally with the zoom, which makes no sense for text labels).
 * Real bug found via feedback: giving both their own `overflow-y-auto`
 * scrolled them INDEPENDENTLY — scrolling the track rows left the labels
 * behind, visibly misaligning each character's name from its own row. Fixed
 * by making the right side (track rows) the only one with a visible
 * scrollbar/wheel handling, and mirroring its scrollTop onto the label
 * column (which has `overflow-hidden`, so it has no scrollbar of its own
 * and can't be scrolled directly — it only ever follows the right side). */
export function Timeline({
  isPlaying,
  onPlayPause,
  onSeek,
  characters,
  dialogueLines,
}: TimelineProps) {
  const timeline = useEditorStore((state) => state.timeline);
  const playheadSeconds = useEditorStore((state) => state.playheadSeconds);
  const pixelsPerSecond = useEditorStore((state) => state.pixelsPerSecond);
  const setZoom = useEditorStore((state) => state.setZoom);
  const updateTrack = useEditorStore((state) => state.updateTrack);
  const selectClip = useEditorStore((state) => state.selectClip);
  const audioLabelsRef = useRef<HTMLDivElement>(null);
  const dialogueLinesById = useMemo(
    () => new Map(dialogueLines.map((line) => [line.id, line])),
    [dialogueLines],
  );

  if (!timeline)
    return <p className="p-4 text-sm text-gray-400">Loading timeline…</p>;

  // Split out the single "video" track so it can be pinned above the
  // scrollable audio tracks (see component docstring above).
  const videoTrack = timeline.tracks.find((track) => track.type === "video");
  const audioTracks = timeline.tracks.filter((track) => track.type !== "video");

  function trackLabel(track: Track): string {
    if (track.type === "dialogue") {
      const character = characters.find((c) => c.id === track.character_id);
      return character?.name ?? "Unknown character";
    }
    return TRACK_TYPE_LABELS[track.type];
  }

  // `pinned` only adds `shrink-0` (so the pinned video label cell can't get
  // squeezed by the flex layout) — the border classes stay the same either
  // way, since the video label is never actually the LAST sibling in the DOM
  // (the scrollable audio-label wrapper always comes right after it), so
  // `last:border-b-0` already only ever strips the border off the truly
  // last AUDIO track label, exactly like before this split.
  function renderTrackLabel(track: Track, pinned: boolean) {
    return (
      <div
        key={track.id}
        className={clsx(
          "flex flex-col justify-center gap-1 truncate border-b px-2 last:border-b-0",
          pinned && "shrink-0",
        )}
        style={{ height: TRACK_HEIGHT_PX }}
        title={trackLabel(track)}
      >
        <span className="truncate text-xs font-medium text-gray-600">
          {trackLabel(track)}
        </span>
        {/* Mute/solo/volume only make sense for AUDIO tracks — matches
            the audio_tracks (!= "video") distinction on the backend in
            editor_export_service._select_audible_tracks. */}
        {track.type !== "video" && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => updateTrack(track.id, { muted: !track.muted })}
              className={clsx(
                "rounded px-1 text-[9px] font-semibold leading-4",
                track.muted
                  ? "bg-red-500 text-white"
                  : "bg-gray-200 text-gray-500",
              )}
              aria-label={track.muted ? "Unmute track" : "Mute track"}
            >
              M
            </button>
            <button
              type="button"
              onClick={() => updateTrack(track.id, { solo: !track.solo })}
              className={clsx(
                "rounded px-1 text-[9px] font-semibold leading-4",
                track.solo
                  ? "bg-yellow-500 text-white"
                  : "bg-gray-200 text-gray-500",
              )}
              aria-label={track.solo ? "Unsolo track" : "Solo track"}
            >
              S
            </button>
            <input
              type="range"
              min={0}
              max={2}
              step={0.1}
              value={track.volume}
              onChange={(e) =>
                updateTrack(track.id, { volume: Number(e.target.value) })
              }
              className="h-1 w-10 accent-blue-600"
              aria-label="Track volume"
            />
          </div>
        )}
      </div>
    );
  }

  function handleRulerClick(e: ReactMouseEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const seconds = Math.max(0, (e.clientX - rect.left) / pixelsPerSecond);
    onSeek(Math.min(seconds, timeline!.duration_seconds));
    // Clicking empty track space (not a clip — ClipBlock.tsx stops this
    // event from bubbling here on its own click) deselects the current
    // clip, closing ClipInspector — same real UX gap as the Escape/close
    // button in VideoEditor.tsx/ClipInspector.tsx: this was the only
    // remaining dead end with no way back to "nothing selected".
    selectClip(null);
  }

  // Mirrors the track rows' scroll position onto the label column — see the
  // component docstring above for why these are 2 separate scroll
  // containers instead of 1. Guarded by the equality check so setting
  // `scrollTop` here (which itself fires a scroll event on the label
  // column) doesn't ping-pong — the label column has no onScroll handler
  // of its own anyway (it's `overflow-hidden`), but the guard is kept
  // regardless as the cheap, obviously-correct thing to do.
  function handleAudioTracksScroll(e: UIEvent<HTMLDivElement>) {
    const labelsEl = audioLabelsRef.current;
    if (labelsEl && labelsEl.scrollTop !== e.currentTarget.scrollTop) {
      labelsEl.scrollTop = e.currentTarget.scrollTop;
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b bg-white px-4 py-2">
        <button
          type="button"
          onClick={onPlayPause}
          className="flex items-center justify-center rounded bg-blue-600 p-1.5 text-white"
          aria-label={isPlaying ? "Pause" : "Play"}
        >
          {isPlaying ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <span className="text-xs tabular-nums text-gray-500">
          {playheadSeconds.toFixed(1)}s / {timeline.duration_seconds.toFixed(1)}
          s
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setZoom(pixelsPerSecond - ZOOM_STEP_PX_PER_SECOND)}
          className="rounded border p-1.5 text-gray-600 hover:bg-gray-50"
          aria-label="Zoom out"
        >
          <ZoomOut size={14} />
        </button>
        <button
          type="button"
          onClick={() => setZoom(pixelsPerSecond + ZOOM_STEP_PX_PER_SECOND)}
          className="rounded border p-1.5 text-gray-600 hover:bg-gray-50"
          aria-label="Zoom in"
        >
          <ZoomIn size={14} />
        </button>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div className="flex w-32 shrink-0 flex-col border-r bg-white">
          {videoTrack && renderTrackLabel(videoTrack, true)}
          <div ref={audioLabelsRef} className="flex-1 overflow-hidden">
            {audioTracks.map((track) => renderTrackLabel(track, false))}
          </div>
        </div>

        <div className="flex-1 overflow-x-auto">
          <div
            className="relative flex h-full cursor-pointer flex-col"
            style={{ width: timeline.duration_seconds * pixelsPerSecond }}
            onClick={handleRulerClick}
          >
            <Playhead positionPx={playheadSeconds * pixelsPerSecond} />
            {videoTrack && (
              <div className="shrink-0 border-b">
                <TrackRow
                  track={videoTrack}
                  pixelsPerSecond={pixelsPerSecond}
                  onSeek={onSeek}
                  dialogueLinesById={dialogueLinesById}
                />
              </div>
            )}
            <div
              className="flex-1 overflow-y-auto"
              onScroll={handleAudioTracksScroll}
            >
              {audioTracks.map((track) => (
                <TrackRow
                  key={track.id}
                  track={track}
                  pixelsPerSecond={pixelsPerSecond}
                  onSeek={onSeek}
                  dialogueLinesById={dialogueLinesById}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
