import { useEffect, useRef } from "react";
import WaveSurfer from "wavesurfer.js";
import { resolvePlayableUrl } from "@/lib/mediaUrl";

interface WaveformClipProps {
  gcsUri: string;
  signedUrlPath: string;
  heightPx: number;
}

/** A miniature waveform drawn ON TOP of a dialogue clip in the timeline
 * (US1: "waveform shown on an audio clip so you can see where there's
 * sound") — uses wavesurfer.js, already available in package.json (see
 * AudioPlayer.tsx, its first use for per-line audio in the Review Dashboard,
 * and lib/mediaUrl.ts for the shared URL-resolving logic). Has NO play/pause
 * button or its own playback cursor — this is purely for VIEWING the
 * waveform shape; playback/scrubbing already exists in Timeline.tsx's shared
 * transport (select the clip, then use the page's shared playhead/play,
 * rather than playing each clip individually like AudioPlayer does).
 *
 * A click on the waveform must still fall through to the `onClick` of the
 * wrapping `<button>` (ClipBlock.tsx, selects the clip) rather than being
 * handled by wavesurfer itself — the mechanism that ACTUALLY guarantees this
 * is the `pointer-events-none` class on the container (last line of this
 * file), which makes the browser skip the entire DOM subtree wavesurfer
 * creates during click hit-testing. `interact: false`/`cursorWidth: 0` only
 * additionally disable wavesurfer's OWN INTERNAL click-to-seek/cursor
 * behavior (it still attaches its own click listener to its wrapper
 * regardless of this option, see the wavesurfer.js renderer — it simply no
 * longer emits a seek event) — this is not a propagation-blocking mechanism,
 * just extra insurance, and shouldn't be relied on alone if the
 * pointer-events-none class is ever changed or removed later.
 *
 * A load error (clip doesn't yet have valid audio) only hides the waveform,
 * it doesn't hide the clip — the clip can still be selected normally thanks
 * to ClipBlock.tsx's own background color. */
export function WaveformClip({
  gcsUri,
  signedUrlPath,
  heightPx,
}: WaveformClipProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;
    let wavesurfer: WaveSurfer | null = null;

    resolvePlayableUrl(gcsUri, signedUrlPath)
      .then((playableUrl) => {
        if (cancelled || !containerRef.current) return;
        wavesurfer = WaveSurfer.create({
          container: containerRef.current,
          waveColor: "#1e3a8a",
          height: heightPx,
          interact: false,
          cursorWidth: 0,
          url: playableUrl,
        });
      })
      .catch(() => {
        // Silent — see docstring: a missing waveform shouldn't block the
        // user from selecting/interacting with the clip.
      });

    return () => {
      cancelled = true;
      wavesurfer?.destroy();
    };
  }, [gcsUri, signedUrlPath, heightPx]);

  return (
    <div ref={containerRef} className="pointer-events-none h-full w-full" />
  );
}
