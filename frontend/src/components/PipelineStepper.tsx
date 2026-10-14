import { Check } from "lucide-react";
import type { JobStatus } from "@/hooks/useJobStatus";

const STAGES = [
  { key: "transcribed", label: "Transcription" },
  { key: "voice_profiled", label: "Voice Profiling" },
  { key: "translated", label: "Translation" },
  { key: "safety_checked", label: "Cultural Safety" },
  { key: "dubbed", label: "Dubbing" },
  { key: "muxed", label: "Audio-Video Mux" },
  { key: "ready_for_review", label: "Ready for Review" },
];

/**
 * The "wow" screen for demos — visually shows the multi-agent pipeline as it
 * runs (see docs/frontend-spec.md section 3.3). Receives data via
 * useJobStatus (Firestore).
 */
export function PipelineStepper({ status }: { status: JobStatus | null }) {
  const currentIndex = STAGES.findIndex((s) => s.key === status?.currentStage);
  // currentIndex is -1 whenever current_stage is a value outside this
  // 7-stage list — "exporting" (Video Editor export) or "muxing" (Attach
  // Video) are the only 2 real values this ever happens for (see
  // worker/main.py), and BOTH are only reachable after the job already
  // passed through every one of these stages at least once (_STAGE_ORDER
  // in worker/pipeline.py always ends at "ready_for_review" before any
  // later export/attach can run) — so -1 unambiguously means "all done,
  // doing something else now", not "hasn't started yet". Found via
  // code-reviewer while fixing the "published" shortcut above: without
  // this, navigating to /jobs/{id}/status during a later export/attach
  // showed every step gray/pending, misleadingly implying nothing had
  // happened yet for an already-fully-processed job.
  const alreadyCompletedPipeline = currentIndex === -1 && !!status;

  return (
    <ol className="flex flex-col gap-3">
      {STAGES.map((stage, index) => {
        // Real bug found via user feedback: this used to also treat EVERY
        // stage as done whenever status?.status === "published" — correct
        // the FIRST time a job completes (status only flips to "published"
        // once current_stage has genuinely reached "ready_for_review"), but
        // wrong for good afterward: a job's `status` stays "published"
        // forever once set (editing a dialogue line, which triggers an
        // automatic redub + re-mux, only rewinds `current_stage` — see
        // update_dialogue_line in api/routers/jobs.py — it never touches
        // `status`). Visiting /jobs/{id}/status again during that LATER
        // redub showed Dubbing correctly "in progress" while Mux/Ready for
        // Review were ALREADY green, from the stale assumption that
        // "published" means the whole pipeline is done. `currentStage`
        // itself is already a reliable, continuously-updated real-time
        // value for this exact "can revisit earlier stages" case (see the
        // reasoning on useJobStatusPollFallback in useJobStatus.ts) — no
        // need for a "published" shortcut at all.
        const isDone = currentIndex > index || alreadyCompletedPipeline;
        const isActive = currentIndex === index;
        return (
          <li key={stage.key} className="flex items-center gap-3">
            <span
              className={`flex h-6 w-6 items-center justify-center rounded-full text-xs ${
                isDone
                  ? "bg-green-500 text-white"
                  : isActive
                    ? "animate-pulse bg-blue-500 text-white"
                    : "bg-gray-200 text-gray-500"
              }`}
            >
              {isDone ? <Check size={14} /> : index + 1}
            </span>
            <span className={isActive ? "font-semibold" : ""}>
              {stage.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
