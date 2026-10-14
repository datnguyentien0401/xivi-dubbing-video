import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { SafetyBadge } from "@/components/SafetyBadge";
import type { DialogueLine } from "@/hooks/useDialogueLines";
import type { Clip, Track } from "@/stores/editorStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface ClipInspectorProps {
  dialogueLine: DialogueLine | null;
  characterName?: string;
  /** The currently selected clip (any track type) — null when no clip is
   * selected. Used to show volume/fade (US3); kept separate from
   * `dialogueLine` because clips on the video/original-audio track have no
   * dialogue line but still have volume/fade. */
  clip: Clip | null;
  /** The track type of the selected clip — only used to decide whether to
   * show the Split/Delete block (US2, "video" track only). */
  trackType: Track["type"] | null;
  /** true if the playhead currently sits inside the body of the selected
   * video clip (far enough from both ends) — used to disable the Split
   * button when clicking it would have no effect (see VideoEditor.tsx; the
   * actual logic lives in editorStore.splitClip). */
  canSplitAtPlayhead: boolean;
  onUpdateClip: (patch: Partial<Clip>) => void;
  /** Splits the selected video clip at the current playhead position (US2). */
  onSplitClip: () => void;
  /** Deletes the selected video clip — true: shifts subsequent clips
   * earlier (ripple); false: leaves a gap on the video track (US2). The
   * warning about losing dubbed dialogue (if any) is asked by
   * VideoEditor.tsx itself BEFORE calling this callback — not the
   * responsibility of this purely presentational component. */
  onDeleteClip: (ripple: boolean) => void;
  /** Deselects the clip, hiding this panel (real UX gap found via feedback:
   * there was no way to close this panel once a clip was selected, besides
   * selecting a DIFFERENT clip — clicking empty timeline space and pressing
   * Escape also call this, see VideoEditor.tsx/Timeline.tsx, but this button
   * is the discoverable one). */
  onClose: () => void;
}

/** The panel "next to" the video preview when a clip is selected on the
 * timeline (US1: "click a dialogue clip → the video jumps to that exact
 * moment, the side panel shows the original text, translated text, and
 * cultural safety label"; US2: Split/Delete+ripple for video clips; US3:
 * volume/fade in/out of the selected clip) — fully hidden when no clip is
 * selected. The original/translated text and safety label only show when
 * the clip has a dialogue line (track "dialogue"); the volume/fade section
 * shows for ANY selected clip type; the Split/Delete block only shows for
 * the "video" track.
 *
 * Not listed among the original files for the US1 task (tasks.md only lists
 * ClipBlock.tsx/WaveformClip.tsx) — split out because this is a clearly
 * different concern (a detail panel next to the video, not drawing clips on
 * the timeline); folding it into ClipBlock.tsx would mix two unrelated
 * responsibilities. */
export function ClipInspector({
  dialogueLine,
  characterName,
  clip,
  trackType,
  canSplitAtPlayhead,
  onUpdateClip,
  onSplitClip,
  onDeleteClip,
  onClose,
}: ClipInspectorProps) {
  // SEPARATE display text for the two fade fields — not bound directly to
  // clip.fade_*_seconds (number), because a controlled input bound to a
  // number would "correct" the string being typed on every re-render (e.g.
  // typing "0.5": after typing ".", Number(".")=0 resets the state to 0 → the
  // input shows "0" again, the just-typed "." is lost — then typing "5"
  // yields "05" instead of "0.5"). Only re-synced from the clip when
  // SWITCHING to a different clip (deps [clip?.id]), not every time
  // fade_in_seconds changes as a side effect of typing itself.
  const [fadeInText, setFadeInText] = useState(() =>
    String(clip?.fade_in_seconds ?? 0),
  );
  const [fadeOutText, setFadeOutText] = useState(() =>
    String(clip?.fade_out_seconds ?? 0),
  );

  useEffect(() => {
    if (clip) {
      setFadeInText(String(clip.fade_in_seconds));
      setFadeOutText(String(clip.fade_out_seconds));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip?.id]);

  if (!clip) return null;

  return (
    <div className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-l bg-card p-4">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Clip details
        </p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close clip details"
          title="Close (Esc)"
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <X size={14} />
        </button>
      </div>

      {dialogueLine && (
        <>
          {characterName && (
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {characterName}
            </p>
          )}
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              Original
            </p>
            <p className="text-sm text-foreground">
              {dialogueLine.original_text}
            </p>
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              Translated
            </p>
            <p className="text-sm text-foreground">
              {dialogueLine.translated_text ?? "—"}
            </p>
          </div>
          <SafetyBadge
            status={dialogueLine.safety_status}
            reasoning={dialogueLine.safety_reasoning}
          />
        </>
      )}

      <div className="flex flex-col gap-2 border-t pt-3">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Volume ({clip.volume.toFixed(1)}x)
          <input
            type="range"
            min={0}
            max={2}
            step={0.1}
            value={clip.volume}
            onChange={(e) => onUpdateClip({ volume: Number(e.target.value) })}
            className="h-1 accent-[hsl(var(--primary))]"
          />
        </label>
        <div className="flex items-center gap-3">
          <label className="flex flex-1 items-center justify-between text-xs text-muted-foreground">
            Fade in (s)
            <Input
              type="number"
              min={0}
              step={0.1}
              value={fadeInText}
              onChange={(e) => {
                setFadeInText(e.target.value);
                const parsed = Number(e.target.value);
                if (!Number.isNaN(parsed))
                  onUpdateClip({ fade_in_seconds: Math.max(0, parsed) });
              }}
              className="ml-2 h-7 w-16 px-1.5 text-xs"
            />
          </label>
          <label className="flex flex-1 items-center justify-between text-xs text-muted-foreground">
            Fade out (s)
            <Input
              type="number"
              min={0}
              step={0.1}
              value={fadeOutText}
              onChange={(e) => {
                setFadeOutText(e.target.value);
                const parsed = Number(e.target.value);
                if (!Number.isNaN(parsed))
                  onUpdateClip({ fade_out_seconds: Math.max(0, parsed) });
              }}
              className="ml-2 h-7 w-16 px-1.5 text-xs"
            />
          </label>
        </div>
      </div>

      {trackType === "video" && (
        <div className="flex flex-col gap-2 border-t pt-3">
          <p className="text-xs font-medium text-muted-foreground">
            Video clip
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={onSplitClip}
            disabled={!canSplitAtPlayhead}
            title={
              canSplitAtPlayhead
                ? undefined
                : "Move the playhead inside this clip to split it"
            }
          >
            Split at playhead
          </Button>
          <div className="flex gap-2">
            <Button
              variant="destructiveOutline"
              size="sm"
              className="flex-1"
              onClick={() => onDeleteClip(true)}
              title="Remove this segment and shift everything after it earlier"
            >
              Delete + ripple
            </Button>
            <Button
              variant="destructiveOutline"
              size="sm"
              className="flex-1"
              onClick={() => onDeleteClip(false)}
              title="Remove this segment, leave a gap (black frame) in its place"
            >
              Delete (leave gap)
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
