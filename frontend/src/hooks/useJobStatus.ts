import { doc, onSnapshot } from "firebase/firestore";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { db } from "@/lib/firebase";
import { apiFetch } from "@/lib/api";
import type { Job } from "@/hooks/useDialogueLines";

export interface JobStatus {
  currentStage: string;
  status: string;
  /** Current export progress (Video Editor, specs/002-video-editor) — only
   * has 2 honest values, 0 or 100 (see
   * worker/main.py::_update_export_status), not a real step-by-step
   * percentage. `undefined` if the document has never written this field
   * (before the first export) OR data is coming via the REST polling
   * fallback (GET /jobs/{id} doesn't have this field — only Firestore
   * does). */
  exportProgress?: number;
  /** Set once an "Attach Video" attempt finishes (specs/003-script-first-
   * ingestion, US5, see worker/main.py::_attach_video /
   * _update_attach_video_status) — `false` means muxing failed and
   * `job.muxed_video_gcs_uri` was NOT set, despite `job.source_video_id`
   * already being populated. `undefined` while no attach has finished yet,
   * OR (same caveat as exportProgress above) while data is coming via the
   * REST polling fallback — that endpoint has no Firestore-only fields. */
  muxSucceeded?: boolean;
  /** A mismatch between the attached video's real duration and what the
   * already-dubbed audio expects (see worker/main.py::_attach_video) —
   * advisory only, set regardless of muxSucceeded. */
  durationWarning?: string;
}

/** REST polling fallback — ONLY enabled while Firestore has never delivered a
 * snapshot (`firestoreStatus === null`). In local dev, the worker deliberately
 * skips writing to Firestore when USE_LOCAL_FIRESTORE=true (see
 * worker/main.py::_update_realtime_status), so the document is never
 * created — there's no other way for the UI to know what stage the job is in
 * besides polling GET /jobs/{id} itself. In the cloud, Firestore usually
 * delivers the first snapshot almost immediately on subscribe (this has
 * always worked correctly), so `firestoreStatus` leaves null very quickly and
 * this polling turns itself off accordingly — this fallback does NOT change
 * cloud behavior/cost compared to before it was added.
 *
 * `refetchInterval` is ALWAYS a fixed 3000ms while enabled — it does NOT turn
 * itself off when `current_stage === "ready_for_review"` like the previous
 * version did (that was a real bug, found while building the Export button:
 * a job can leave "ready_for_review" and then RETURN to that exact stage
 * multiple times over its life (each export is a
 * ready_for_review → exporting → ready_for_review cycle), not the ONE-WAY
 * progression of the original dubbing pipeline that this condition was
 * originally written for — turning off polling the first time
 * "ready_for_review" is seen would mean the UI permanently never sees
 * subsequent "exporting" states again, even though the job genuinely changes
 * stage on the server. The cost of unbounded polling while `enabled` is
 * acceptable — this case ONLY happens in local dev without real Firestore
 * (localhost, negligible cost); in the cloud, `firestoreStatus` leaves null
 * almost instantly so this fallback turns off via `enabled` and never polls
 * for long. */
function useJobStatusPollFallback(
  jobId: string | undefined,
  firestoreStatus: JobStatus | null,
) {
  return useQuery({
    queryKey: ["job-status-poll-fallback", jobId],
    queryFn: () => apiFetch<Job>(`/jobs/${jobId}`),
    enabled: !!jobId && firestoreStatus === null,
    refetchInterval: 3000,
  });
}

/**
 * Firestore realtime listener for job status (primary source — see
 * docs/frontend-spec.md section 3.3, frontend AGENTS.md), with a REST polling
 * fallback when Firestore isn't available (see useJobStatusPollFallback).
 */
export function useJobStatus(jobId: string | undefined): JobStatus | null {
  const [status, setStatus] = useState<JobStatus | null>(null);

  useEffect(() => {
    if (!jobId) return;
    setStatus(null); // reset when jobId changes — avoid briefly showing the previous job's status
    const unsubscribe = onSnapshot(doc(db, "jobs", jobId), (snapshot) => {
      const data = snapshot.data();
      if (data)
        setStatus({
          currentStage: data.currentStage,
          status: data.status,
          exportProgress: data.exportProgress,
          muxSucceeded: data.muxSucceeded,
          durationWarning: data.durationWarning,
        });
    });
    return () => unsubscribe();
  }, [jobId]);

  const { data: polled } = useJobStatusPollFallback(jobId, status);

  if (status) return status;
  if (polled)
    return { currentStage: polled.current_stage, status: polled.status };
  return null;
}
