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
// The only 2 real current_stage values that are both (a) outside the 7-stage
// list below and (b) only reachable after the job already passed through
// every one of those stages at least once — "exporting" (Video Editor
// export) and "muxing" (Attach Video), see worker/main.py. _STAGE_ORDER in
// worker/pipeline.py always ends at "ready_for_review" before any later
// export/attach can run, so seeing either of these means "all done, doing
// something else now". This must be an explicit allowlist, NOT "anything not
// in STAGES" — current_stage also starts as "queued" (the Job model's own
// default, see api/db/models.py) before the worker has picked up a
// brand-new job, which is equally outside the 7-stage list but means the
// exact opposite (hasn't started yet). Real bug found via user feedback:
// treating every not-in-list value as "done" showed every step green on a
// job's very first run.
const POST_COMPLETION_STAGES = new Set(["exporting", "muxing"]);

export function PipelineStepper({ status }: { status: JobStatus | null }) {
  const currentIndex = STAGES.findIndex((s) => s.key === status?.currentStage);
  const alreadyCompletedPipeline =
    !!status && POST_COMPLETION_STAGES.has(status.currentStage);

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
        //
        // `isActive` highlights the row AFTER currentIndex, not currentIndex
        // itself — another real bug found via user feedback ("stuck at
        // Dubbing for a long time" when the job had actually already moved
        // on to Mux). `context.current_stage` (worker/pipeline.py) is
        // written ONLY AFTER a stage's agent finishes — it is a
        // last-completed checkpoint (needed as-is for crash/resume, see
        // _stage_index in pipeline.py), not a "this is running right now"
        // signal. The pipeline's for-loop moves on to the NEXT stage
        // immediately once a checkpoint is saved, so whatever is actually
        // executing at any moment is always 1 stage ahead of the value this
        // component receives — e.g. currentStage "dubbed" means Dubbing
        // already finished and Mux (the slow, CPU-bound ffmpeg step) is what
        // is really in progress. Shifting the active marker forward by one
        // index displays the stage that is actually running, including
        // Transcription itself right after job creation (currentStage
        // "queued", outside STAGES — see POST_COMPLETION_STAGES above —
        // correctly maps to index 0 being active instead of nothing).
        const isDone = currentIndex >= index || alreadyCompletedPipeline;
        const isActive =
          !!status && !alreadyCompletedPipeline && currentIndex + 1 === index;
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
