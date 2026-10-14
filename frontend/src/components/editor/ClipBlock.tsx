import { useRef } from "react";
import clsx from "clsx";
import type { DialogueLine } from "@/hooks/useDialogueLines";
import { useEditorStore, type Clip, type Track } from "@/stores/editorStore";
import { WaveformClip } from "@/components/editor/WaveformClip";

const TRACK_COLORS: Record<Track["type"], string> = {
  video: "bg-slate-300 border-slate-400",
  audio_original: "bg-amber-200 border-amber-400",
  dialogue: "bg-blue-200 border-blue-400",
};

// Below this threshold, the waveform no longer has enough room to draw
// meaningfully (only a few pixels left) — skip it outright instead of
// creating an almost-useless wavesurfer instance.
const MIN_WAVEFORM_WIDTH_PX = 12;
const WAVEFORM_HEIGHT_PX = 40;
// Width of the hit-test area on the two edges of a clip used for trimming —
// wide enough to hit reliably with the mouse but without eating too much
// into the move-drag area in the middle.
const EDGE_HANDLE_WIDTH_PX = 6;
const MIN_CLIP_DURATION_SECONDS = 0.1;
// Distance (px) for an edge being dragged to "snap" to another clip's edge
// or the playhead — converted to seconds using the current pixelsPerSecond
// before comparing.
const SNAP_THRESHOLD_PX = 8;
// Below this movement threshold (seconds), treat it as a plain click
// (select the clip) rather than creating a junk undo step for a gesture
// that didn't actually drag anywhere.
const DRAG_NOOP_THRESHOLD_SECONDS = 0.02;

interface ClipBlockProps {
  clip: Clip;
  track: Track;
  pixelsPerSecond: number;
  onSeek: (seconds: number) => void;
  /** The corresponding dialogue line — only set for clips on the "dialogue"
   * track (see TrackRow.tsx, looked up via clip.dialogue_line_id). */
  dialogueLine?: DialogueLine;
  /** Drag the clip body to change its position (US3) — DIALOGUE clips only
   * (see TrackRow.tsx). Video/original-audio clips CANNOT be moved — US2
   * only allows split/trim/delete; there is no "freely reposition" action
   * for the video track in spec.md. */
  canMove: boolean;
  /** Drag the left/right edge to trim (US2 for the "video" track, US3 for
   * the "dialogue" track — see TrackRow.tsx). Original audio is currently
   * out of scope (a single full-length clip, no editing action defined in
   * spec.md). */
  canTrim: boolean;
}

type DragMode = "move" | "trim-start" | "trim-end";

interface DragStart {
  mode: DragMode;
  pointerId: number;
  startClientX: number;
  startTimelineStart: number;
  startSourceStart: number;
  startSourceEnd: number;
  /** Adjacent edges on the SAME track at the moment the drag started
   * (snapshot — not re-read from the store mid-drag, since the track.clips
   * prop is already "frozen" at the pointerdown render, matching the
   * semantics of 1 drag gesture = 1 operation). */
  neighbors: { id: string; start: number; end: number }[];
}

function clipEnd(clip: Clip): number {
  return clip.timeline_start + (clip.source_end - clip.source_start);
}

/** A single clip on the timeline (US1 + US2 + US3) — selects + seeks on
 * click; if `canMove`, also drags to reposition (clip body, dialogue clips
 * only); if `canTrim`, drags the left/right edge to resize the duration
 * (dialogue clips AND video clips). Both actions snap to other clips'
 * edges/the playhead, block overlap with adjacent clips, and block going
 * past the timeline bounds. Draws the waveform if it's a dialogue clip that
 * already has audio (see WaveformClip.tsx), and shows a yellow border if the
 * dialogue line is flagged for cultural safety (US4, but surfaced early from
 * US1 since the data is already available — see AGENTS.md principle IV,
 * "transparency of AI decisions"). The FULL original/translated text and
 * safety label are shown in a separate panel (see ClipInspector.tsx inside
 * VideoEditor.tsx) — the clip itself is too small to display the full text
 * directly.
 *
 * While dragging: updates the DOM style directly (no setState) for smooth
 * per-pixel movement, committing to the store (updateClip) exactly once on
 * pointerup — each drag gesture = exactly one undo step, not dozens of steps
 * per pixel. Limitation: does NOT block trim-end from exceeding the real
 * length of the dubbed audio file (the client has no way to know this value
 * in advance) — it only blocks overlap with adjacent clips and the timeline
 * bounds; if dragged longer than the source audio, on export you'll simply
 * hear the real audio end then silence, with no render error (ffmpeg stops
 * automatically once the source data runs out). Acceptable for the MVP — the
 * main case that needs support is SHORTENING (trimming audio that's longer
 * than the frame), not lengthening.
 */
export function ClipBlock({
  clip,
  track,
  pixelsPerSecond,
  onSeek,
  dialogueLine,
  canMove,
  canTrim,
}: ClipBlockProps) {
  const selectedClipId = useEditorStore((state) => state.selectedClipId);
  const selectClip = useEditorStore((state) => state.selectClip);
  const updateClip = useEditorStore((state) => state.updateClip);
  const playheadSeconds = useEditorStore((state) => state.playheadSeconds);
  const timelineDuration = useEditorStore(
    (state) => state.timeline?.duration_seconds ?? Infinity,
  );

  const elementRef = useRef<HTMLButtonElement>(null);
  const dragRef = useRef<DragStart | null>(null);

  const duration = clip.source_end - clip.source_start;
  const isSelected = clip.id === selectedClipId;
  const widthPx = Math.max(duration * pixelsPerSecond, 4);
  const isFlagged = dialogueLine?.safety_status === "flagged";

  function snapSeconds(value: number, excludeIds: Set<string>): number {
    const thresholdSeconds = SNAP_THRESHOLD_PX / pixelsPerSecond;
    const candidates = [0, timelineDuration, playheadSeconds];
    for (const other of track.clips) {
      if (excludeIds.has(other.id)) continue;
      candidates.push(other.timeline_start, clipEnd(other));
    }
    let best = value;
    let bestDist = thresholdSeconds;
    for (const candidate of candidates) {
      const dist = Math.abs(candidate - value);
      if (dist < bestDist) {
        best = candidate;
        bestDist = dist;
      }
    }
    return best;
  }

  function startDrag(mode: DragMode) {
    return (e: React.PointerEvent<HTMLDivElement | HTMLButtonElement>) => {
      if (mode === "move" ? !canMove : !canTrim) return;
      e.stopPropagation();
      e.preventDefault();
      // Pointer capture is required — otherwise, releasing the mouse OUTSIDE
      // the browser window (dragging right to the edge of the screen) would
      // never fire a pointerup event, leaving dragRef stuck in the dragging
      // state forever, with the pointermove listener on window continuing to
      // "hijack" every subsequent mouse movement until the page is refreshed.
      e.currentTarget.setPointerCapture(e.pointerId);
      selectClip(clip.id);

      const neighbors = track.clips
        .filter((c) => c.id !== clip.id)
        .map((c) => ({ id: c.id, start: c.timeline_start, end: clipEnd(c) }));

      dragRef.current = {
        mode,
        pointerId: e.pointerId,
        startClientX: e.clientX,
        startTimelineStart: clip.timeline_start,
        startSourceStart: clip.source_start,
        startSourceEnd: clip.source_end,
        neighbors,
      };

      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp);
    };
  }

  /** Adjacent preceding/following edges of the clip being dragged (compared
   * against the original timeline_start) — constrains trimming so it can't
   * "jump over" another clip, only resize up to the adjacent edge. */
  function findAdjacent(drag: DragStart) {
    let prevEnd = 0;
    let nextStart = timelineDuration;
    for (const n of drag.neighbors) {
      if (n.end <= drag.startTimelineStart && n.end > prevEnd) prevEnd = n.end;
      if (
        n.start >=
          drag.startTimelineStart +
            (drag.startSourceEnd - drag.startSourceStart) &&
        n.start < nextStart
      )
        nextStart = n.start;
    }
    return { prevEnd, nextStart };
  }

  /** Greedy "move" collision resolution: if the proposed position overlaps
   * another clip, push it to whichever side is closer (before or after that
   * clip) — good enough for the MVP, may not be optimal with a dense
   * arrangement of many overlapping proposed positions. */
  function clampMove(desiredStart: number, drag: DragStart): number {
    const clipDuration = drag.startSourceEnd - drag.startSourceStart;
    let start = Math.max(
      0,
      Math.min(desiredStart, timelineDuration - clipDuration),
    );
    for (const n of drag.neighbors) {
      const end = start + clipDuration;
      if (start < n.end && end > n.start) {
        const pushLeft = n.start - clipDuration;
        const pushRight = n.end;
        start =
          Math.abs(desiredStart - pushLeft) < Math.abs(desiredStart - pushRight)
            ? Math.max(0, pushLeft)
            : Math.min(pushRight, timelineDuration - clipDuration);
      }
    }
    return start;
  }

  function computeMove(deltaSeconds: number, drag: DragStart) {
    const desired = drag.startTimelineStart + deltaSeconds;
    const snapped = snapSeconds(desired, new Set([clip.id]));
    const clamped = clampMove(snapped, drag);
    return clamped;
  }

  function computeTrimStart(deltaSeconds: number, drag: DragStart) {
    const { prevEnd } = findAdjacent(drag);
    const clipDuration = drag.startSourceEnd - drag.startSourceStart;
    const maxStart =
      drag.startTimelineStart + clipDuration - MIN_CLIP_DURATION_SECONDS;
    let newStart = drag.startTimelineStart + deltaSeconds;
    newStart = snapSeconds(newStart, new Set([clip.id]));
    newStart = Math.max(prevEnd, Math.min(newStart, maxStart));
    const appliedDelta = newStart - drag.startTimelineStart;
    return {
      timelineStart: newStart,
      sourceStart: drag.startSourceStart + appliedDelta,
    };
  }

  function computeTrimEnd(deltaSeconds: number, drag: DragStart) {
    const { nextStart } = findAdjacent(drag);
    const clipDuration = drag.startSourceEnd - drag.startSourceStart;
    const desiredEndPos = drag.startTimelineStart + clipDuration + deltaSeconds;
    let snappedEndPos = snapSeconds(desiredEndPos, new Set([clip.id]));
    snappedEndPos = Math.min(snappedEndPos, nextStart);
    const newDuration = Math.max(
      MIN_CLIP_DURATION_SECONDS,
      snappedEndPos - drag.startTimelineStart,
    );
    return { sourceEnd: drag.startSourceStart + newDuration };
  }

  function handlePointerMove(ev: PointerEvent) {
    const drag = dragRef.current;
    const el = elementRef.current;
    if (!drag || !el || ev.pointerId !== drag.pointerId) return;
    const deltaSeconds = (ev.clientX - drag.startClientX) / pixelsPerSecond;

    if (drag.mode === "move") {
      const newStart = computeMove(deltaSeconds, drag);
      el.style.left = `${newStart * pixelsPerSecond}px`;
    } else if (drag.mode === "trim-start") {
      const { timelineStart, sourceStart } = computeTrimStart(
        deltaSeconds,
        drag,
      );
      el.style.left = `${timelineStart * pixelsPerSecond}px`;
      el.style.width = `${Math.max((drag.startSourceEnd - sourceStart) * pixelsPerSecond, 4)}px`;
    } else {
      const { sourceEnd } = computeTrimEnd(deltaSeconds, drag);
      el.style.width = `${Math.max((sourceEnd - drag.startSourceStart) * pixelsPerSecond, 4)}px`;
    }
  }

  /** Resets the DOM back to the current position/width of the `clip` prop
   * (the value BEFORE the drag, since the branch calling this function
   * doesn't commit updateClip) — must be called explicitly rather than
   * relying on some later re-render to "accidentally" fix it, because
   * handlePointerMove mutates the DOM style directly, so even below the
   * no-op threshold a slight hand tremor on click can leave a few pixels of
   * drift that won't self-correct until this component's own next
   * re-render. */
  function resetElementStyle() {
    const el = elementRef.current;
    if (!el) return;
    el.style.left = `${clip.timeline_start * pixelsPerSecond}px`;
    el.style.width = `${widthPx}px`;
  }

  function handlePointerUp(ev: PointerEvent) {
    window.removeEventListener("pointermove", handlePointerMove);
    window.removeEventListener("pointerup", handlePointerUp);
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag || ev.pointerId !== drag.pointerId) return;
    const deltaSeconds = (ev.clientX - drag.startClientX) / pixelsPerSecond;

    if (drag.mode === "move") {
      const newStart = computeMove(deltaSeconds, drag);
      if (
        Math.abs(newStart - drag.startTimelineStart) <
        DRAG_NOOP_THRESHOLD_SECONDS
      ) {
        resetElementStyle();
        return;
      }
      updateClip(track.id, clip.id, { timeline_start: newStart });
    } else if (drag.mode === "trim-start") {
      const { timelineStart, sourceStart } = computeTrimStart(
        deltaSeconds,
        drag,
      );
      if (
        Math.abs(timelineStart - drag.startTimelineStart) <
        DRAG_NOOP_THRESHOLD_SECONDS
      ) {
        resetElementStyle();
        return;
      }
      updateClip(track.id, clip.id, {
        timeline_start: timelineStart,
        source_start: sourceStart,
      });
    } else {
      const { sourceEnd } = computeTrimEnd(deltaSeconds, drag);
      if (
        Math.abs(sourceEnd - drag.startSourceEnd) < DRAG_NOOP_THRESHOLD_SECONDS
      ) {
        resetElementStyle();
        return;
      }
      updateClip(track.id, clip.id, { source_end: sourceEnd });
    }
  }

  return (
    <button
      ref={elementRef}
      type="button"
      onPointerDown={startDrag("move")}
      onClick={(e) => {
        // Block bubbling up to Timeline.tsx's click-to-seek handler —
        // otherwise, clicking a clip would seek based on the click's X
        // coordinate (correct for empty space) RIGHT AFTER already seeking
        // correctly to clip.timeline_start here, causing the playhead to
        // jump to the wrong position. Not affected by the
        // onPointerDown/preventDefault above — click is a separate event,
        // not cancelled by preventDefault() on pointerdown.
        e.stopPropagation();
        selectClip(clip.id);
        onSeek(clip.timeline_start);
      }}
      title={dialogueLine?.original_text ?? `${duration.toFixed(2)}s`}
      className={clsx(
        "absolute bottom-1 top-1 overflow-hidden rounded border",
        TRACK_COLORS[track.type],
        isSelected && "ring-2 ring-blue-600",
        isFlagged && "border-2 border-yellow-500",
        canMove && "cursor-grab active:cursor-grabbing",
      )}
      style={{
        left: clip.timeline_start * pixelsPerSecond,
        // 4px floor so extremely short clips remain clickable — at the
        // lowest zoom level (MIN_PIXELS_PER_SECOND=10, see editorStore.ts)
        // this can make two ADJACENT clips (valid — the schema only
        // forbids overlap, not being flush against each other) look like
        // they overlap even though the underlying data doesn't overlap at
        // all — acceptable for US1 (view/select only, no drag/trim yet);
        // real dialogue lines are rarely shorter than 0.4s (= 4px / 10px/s)
        // so the actual collision risk is low.
        width: widthPx,
      }}
    >
      {canTrim && (
        <>
          <div
            onPointerDown={startDrag("trim-start")}
            className="absolute inset-y-0 left-0 z-10 cursor-ew-resize hover:bg-black/20"
            style={{ width: EDGE_HANDLE_WIDTH_PX }}
          />
          <div
            onPointerDown={startDrag("trim-end")}
            className="absolute inset-y-0 right-0 z-10 cursor-ew-resize hover:bg-black/20"
            style={{ width: EDGE_HANDLE_WIDTH_PX }}
          />
        </>
      )}
      {dialogueLine?.dubbed_audio_gcs_uri &&
        widthPx >= MIN_WAVEFORM_WIDTH_PX && (
          <WaveformClip
            gcsUri={dialogueLine.dubbed_audio_gcs_uri}
            signedUrlPath={`/dialogue-lines/${dialogueLine.id}/audio-url`}
            heightPx={WAVEFORM_HEIGHT_PX}
          />
        )}
    </button>
  );
}
