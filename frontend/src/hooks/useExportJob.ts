import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Job } from "@/hooks/useDialogueLines";

/** Triggers the final video export from the current timeline (US6,
 * specs/002-video-editor) — just publishes the job for the worker to render
 * (`POST /jobs/{id}/export` returns 202 immediately, without waiting for
 * ffmpeg to finish). Export progress is read via the existing Firestore
 * listener (`useJobStatus.ts`) — do NOT add a separate polling query here
 * (see frontend/AGENTS.md: realtime job/pipeline status must go through
 * Firestore, not polling). */
export function useExportJob(jobId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<Job>(`/jobs/${jobId}/export`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      // Do NOT invalidate ["job-timeline", jobId] here (this was done before,
      // and a real bug was found while testing VideoEditor.tsx in the
      // browser): the job switches to current_stage="exporting" IMMEDIATELY
      // once POST /export is accepted (see
      // backend/api/routers/jobs.py::export_job), and GET /jobs/{id}/timeline
      // itself returns 409 during that window (only allowed when
      // ready_for_review) — refetching right then makes `useJobTimeline` in
      // VideoEditor.tsx fall into isError, showing the "Could not open
      // editor" error screen instead of continuing to show the editor with
      // the export in progress. It's also no longer needed: now that real
      // autosave exists (see useAutosaveTimeline), the client always holds
      // the latest version via editorStore, without depending on refetching
      // this query mid-session (see loadedJobIdRef in VideoEditor.tsx).
    },
  });
}
