import type { DialogueLine } from "@/hooks/useDialogueLines";
import type { Track } from "@/stores/editorStore";
import { ClipBlock } from "@/components/editor/ClipBlock";

export const TRACK_HEIGHT_PX = 56;

interface TrackRowProps {
  track: Track;
  pixelsPerSecond: number;
  onSeek: (seconds: number) => void;
  dialogueLinesById: Map<string, DialogueLine>;
}

/** A single lane on the timeline — pure layout, draws one `ClipBlock` per
 * clip (see ClipBlock.tsx for select/seek/waveform/drag-trim logic). Drag to
 * reposition (US3) is only enabled for the "dialogue" track; drag-edge-to-
 * trim is enabled for BOTH "dialogue" (US3) AND "video" (US2) — original
 * audio is currently out of scope in spec.md (a single full-length clip, no
 * editing action required). Split/delete+ripple (US2) is not a drag action —
 * see the buttons in ClipInspector.tsx. */
export function TrackRow({
  track,
  pixelsPerSecond,
  onSeek,
  dialogueLinesById,
}: TrackRowProps) {
  return (
    <div
      className="relative border-b last:border-b-0"
      style={{ height: TRACK_HEIGHT_PX }}
    >
      {track.clips.map((clip) => (
        <ClipBlock
          key={clip.id}
          clip={clip}
          track={track}
          pixelsPerSecond={pixelsPerSecond}
          onSeek={onSeek}
          canMove={track.type === "dialogue"}
          canTrim={track.type === "dialogue" || track.type === "video"}
          dialogueLine={
            clip.dialogue_line_id
              ? dialogueLinesById.get(clip.dialogue_line_id)
              : undefined
          }
        />
      ))}
    </div>
  );
}
