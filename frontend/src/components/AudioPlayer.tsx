import { useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import WaveSurfer from "wavesurfer.js";
import { resolvePlayableUrl } from "@/lib/mediaUrl";

interface AudioPlayerProps {
  /** URI in the form gs://bucket/path (production) or local://blob_name
   * (local dev, see backend/worker/services/local_storage_client.py) — not
   * yet a directly playable URL, see the resolvePlayableUrl function. */
  gcsUri: string;
  /** Backend path that returns a signed read URL for this exact gcsUri (e.g.
   * `/dialogue-lines/{id}/audio-url`, `/characters/{id}/voice-sample-url`) —
   * ONLY needed when gcsUri is a real gs:// (the GCS bucket is not publicly
   * readable; reproduced a 403 by hand while testing on cloud when building
   * the URL https://storage.googleapis.com/... manually). Not needed when
   * gcsUri is local://. */
  signedUrlPath?: string;
}

/** Compact audio player using wavesurfer.js to review each dubbed dialogue
 * line (see docs/frontend-spec.md section 3.4 — Review Dashboard). */
export function AudioPlayer({ gcsUri, signedUrlPath }: AudioPlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wavesurferRef = useRef<WaveSurfer | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;
    let wavesurfer: WaveSurfer | null = null;
    setIsReady(false);
    setError(false);

    resolvePlayableUrl(gcsUri, signedUrlPath)
      .then((playableUrl) => {
        if (cancelled || !containerRef.current) return;
        wavesurfer = WaveSurfer.create({
          container: containerRef.current,
          waveColor: "#94a3b8",
          progressColor: "#2563eb",
          height: 32,
          url: playableUrl,
        });
        wavesurferRef.current = wavesurfer;
        wavesurfer.on("ready", () => setIsReady(true));
        wavesurfer.on("play", () => setIsPlaying(true));
        wavesurfer.on("pause", () => setIsPlaying(false));
        wavesurfer.on("finish", () => setIsPlaying(false));
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });

    return () => {
      cancelled = true;
      wavesurfer?.destroy();
      wavesurferRef.current = null;
    };
  }, [gcsUri, signedUrlPath]);

  if (error)
    return <span className="text-xs text-red-600">Failed to load audio</span>;

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={!isReady}
        onClick={() => wavesurferRef.current?.playPause()}
        className="flex items-center justify-center rounded bg-blue-600 p-1.5 text-white disabled:opacity-50"
        aria-label={isPlaying ? "Pause" : "Play"}
      >
        {isPlaying ? <Pause size={12} /> : <Play size={12} />}
      </button>
      <div ref={containerRef} className="w-32" />
    </div>
  );
}
