import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Character } from "@/hooks/useCharacters";

export interface DialogueLine {
  id: string;
  character_id: string | null;
  start_time: number;
  end_time: number;
  original_text: string;
  translated_text: string | null;
  is_song: boolean;
  safety_status: "pending" | "ok" | "flagged";
  safety_reasoning: string | null;
  dubbed_audio_gcs_uri: string | null;
}

export interface Job {
  id: string;
  project_id: string;
  // null for a script-first job with no video attached yet
  // (specs/003-script-first-ingestion) — see backend/api/schemas/schemas.py::JobOut.
  source_video_id: string | null;
  target_language: string;
  current_stage: string;
  status: string;
  muxed_video_gcs_uri: string | null;
  // Video exported from the Video Editor (specs/002-video-editor) — null if
  // never exported, see backend/api/schemas/schemas.py::JobOut.
  exported_video_gcs_uri: string | null;
}

/** Creates one job per target language (POST /jobs returns a list — see
 * docs/frontend-spec.md section 3.2: submitting NewProject -> calls POST
 * /jobs -> navigates to Pipeline Status). */
export function useCreateJobs() {
  return useMutation({
    mutationFn: (payload: {
      source_video_id: string;
      target_languages: string[];
    }) =>
      apiFetch<Job[]>("/jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  });
}

export interface ScriptLineImport {
  segment_code: string;
  character_name: string;
  text: string;
}

/** Creates one job per target language directly from an uploaded script —
 * no source video required yet (specs/003-script-first-ingestion, US1). */
export function useCreateScriptJobs() {
  return useMutation({
    mutationFn: (payload: {
      projectId: string;
      lines: ScriptLineImport[];
      target_languages: string[];
      character_voice_assignments: Record<string, { voice_name: string }>;
    }) =>
      apiFetch<Job[]>(`/projects/${payload.projectId}/script-jobs`, {
        method: "POST",
        body: JSON.stringify({
          lines: payload.lines,
          target_languages: payload.target_languages,
          character_voice_assignments: payload.character_voice_assignments,
        }),
      }),
  });
}

/** Signed URL to view the final muxed video — previously there was no way to
 * view the result video (ReviewDashboard.tsx only let you listen to each
 * line's audio). Returns 404 when the job hasn't reached the muxed step yet —
 * callers pass `enabled` to only fetch once it could plausibly exist (e.g.
 * current_stage === "ready_for_review"), avoiding useless 404 error logs
 * while the job is still processing. */
export function useJobVideoUrl(jobId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["job-video-url", jobId],
    queryFn: () => apiFetch<{ url: string }>(`/jobs/${jobId}/video-url`),
    enabled: !!jobId && enabled,
    retry: false,
  });
}

/** Fetches a single job via GET /jobs/{id} — used in ReviewDashboard.tsx to
 * know job.status === "published" even when the user RELOADS the review page
 * after approving in an earlier visit (approveJob.isSuccess is only true
 * within the SAME session right after clicking Approve, and is lost
 * immediately after reload since it's just a local mutation flag, not real
 * server state). */
export function useJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => apiFetch<Job>(`/jobs/${jobId}`),
    enabled: !!jobId,
    // Poll while the job is anywhere other than its resting state
    // ("ready_for_review") — covers 2 distinct transient-stage cases with
    // one condition: (1) a video being attached (specs/003-script-first-
    // ingestion US5, see POST /jobs/{id}/attach-video) — "muxing" is a
    // transient stage the worker always moves away from (back to
    // "ready_for_review", on both success and failure); (2) editing a
    // dialogue line's text/character triggers an automatic redub + re-mux
    // as a pipeline resume (see api/routers/jobs.py::update_dialogue_line's
    // docstring — current_stage is rewound to "safety_checked" synchronously
    // by that PATCH, then the worker runs it back through "dubbed"/"muxed"
    // to "ready_for_review"). Without polling through case (2) as well,
    // muxed_video_gcs_uri's change (a fresh GCS blob each mux, see
    // mux_service.py) was never observed here, so nothing ever told the
    // video preview a new version existed — found via user feedback: the
    // backend was already correctly re-muxing automatically after an edit,
    // but the on-screen video stayed stale until a manual reload. Same
    // polling pattern as the "pending redub" case in useDialogueLines below.
    // Tried driving this off the Firestore-backed useJobStatus instead (per
    // frontend/AGENTS.md's realtime-over-polling preference) and found it
    // genuinely unreliable for this specific case: useJobStatus's own
    // REST-polling fallback (the only option in local dev, where the worker
    // skips writing to Firestore — see useJobStatus.ts) only samples every
    // 3s, so a FAST muxing→ready_for_review round trip (e.g. an immediate
    // failure) can complete entirely between two samples, meaning the
    // observed value never visibly changes and nothing re-fires. Polling the
    // SAME query this component actually renders doesn't have that gap —
    // confirmed by reproducing the fast-failure case live and watching the
    // UI get stuck on the Firestore-driven version before switching to this.
    //
    // Does NOT exclude "exporting" (specs/002-video-editor), even though
    // VideoEditor.tsx (one consumer of this same hook) already has its own
    // explicit invalidation of this query once an export finishes (see its
    // useEffect watching jobStatus/exported_video_gcs_uri) — an earlier
    // version of this condition excluded it for exactly that reason, but
    // that created a real regression found via code-reviewer: ReviewDashboard
    // ALSO calls useJob and locks its editable controls while
    // current_stage !== "ready_for_review" (see the comment on
    // ReviewDashboard.tsx's `editingLocked`) — a user who starts an export in
    // VideoEditor then clicks "Back to Review" before it finishes would land
    // on a page that fetched current_stage="exporting" ONCE and then never
    // polled again, leaving every row's editors permanently disabled with no
    // way to notice the export actually finished short of a full reload.
    // Keeping "exporting" in the poll trades a few redundant requests on
    // VideoEditor's side (harmless — same query, same cache) for closing
    // that stuck-UI gap on ReviewDashboard's side.
    refetchInterval: (query) => {
      const stage = query.state.data?.current_stage;
      return stage && stage !== "ready_for_review" ? 3000 : false;
    },
  });
}

/** Attach a real video to an already-dubbed script-first job
 * (specs/003-script-first-ingestion, US5) — the video itself is uploaded the
 * same way as any other source video (useCreateUploadUrl), this just links
 * an already-uploaded SourceVideo to the job. */
export function useAttachVideo(jobId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { source_video_id: string }) =>
      apiFetch<Job>(`/jobs/${jobId}/attach-video`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    // Writes the response directly into the cache (synchronously, no extra
    // round trip) instead of only invalidating — found via code-reviewer,
    // review 2026-10-11: invalidateQueries alone leaves a brief window
    // (one HTTP round trip) where the cached `job` still shows the PRE-attach
    // snapshot (current_stage still "ready_for_review", no "muxing") even
    // though the backend has already committed the transition — the
    // mutation's own response already carries the fresh current_stage
    // ("muxing"), so there's no reason to wait on a second fetch for it.
    onSuccess: (data) => {
      queryClient.setQueryData(["job", jobId], data);
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
}

export interface ShareUrlResponse {
  url: string;
  expires_in_seconds: number;
}

/** Share/view link for the result video — with a much longer expiry than
 * useJobVideoUrl (see api/routers/jobs.py::get_job_share_url), used for the
 * Copy Link/Share button in ReviewDashboard.tsx. Only enabled once the job is
 * published (the backend also returns 409 if called too early — see AGENTS.md
 * principle #5). */
export function useJobShareUrl(jobId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["job-share-url", jobId],
    queryFn: () => apiFetch<ShareUrlResponse>(`/jobs/${jobId}/share-url`),
    enabled: !!jobId && enabled,
    retry: false,
  });
}

/** Downloads the result video to the local machine — kept separate from
 * useJobShareUrl (an automatic query) because each Download click needs a
 * NEW signed URL carrying response_disposition=attachment (a different file
 * name than the regular view link, see get_job_share_url), which shouldn't
 * share a query key cache with the view/copy link. */
export function useDownloadJobVideo(jobId: string | undefined) {
  return useMutation({
    mutationFn: () =>
      apiFetch<ShareUrlResponse>(`/jobs/${jobId}/share-url?download=true`),
  });
}

/** Zips every dubbed line's audio (named by segment_code) and returns a
 * signed URL to it (FR-008, specs/003-script-first-ingestion) — the
 * deliverable for a script-first job before a video exists, so the editor
 * has something to cut against. A mutation (not a query) for the same
 * reason as useDownloadJobVideo above: each click should fetch a fresh
 * signed URL rather than share a cached one. */
export function useDownloadDubbedAudioBundle(jobId: string | undefined) {
  return useMutation({
    mutationFn: () =>
      apiFetch<ShareUrlResponse>(`/jobs/${jobId}/dubbed-audio-bundle`),
  });
}

export function useDialogueLines(jobId: string | undefined) {
  return useQuery({
    queryKey: ["dialogue-lines", jobId],
    queryFn: () => apiFetch<DialogueLine[]>(`/jobs/${jobId}/dialogue-lines`),
    enabled: !!jobId,
    // Light polling while any line is still waiting to be re-dubbed
    // (translated_text present but no audio yet — e.g. just changed the voice
    // or edited the text via useUpdateDialogueLine, see
    // api/routers/jobs.py::update_dialogue_line) so the table picks up the
    // new audio automatically without the user needing to refresh the page.
    refetchInterval: (query) => {
      const lines = query.state.data;
      const hasPendingRedub = lines?.some(
        (l) =>
          l.safety_status === "ok" &&
          l.translated_text &&
          !l.dubbed_audio_gcs_uri,
      );
      return hasPendingRedub ? 3000 : false;
    },
  });
}

/** List of characters belonging to this job's project — used for the
 * voice/character dropdown for each dialogue line in ReviewDashboard.tsx. */
export function useJobCharacters(jobId: string | undefined) {
  return useQuery({
    queryKey: ["job-characters", jobId],
    queryFn: () => apiFetch<Character[]>(`/jobs/${jobId}/characters`),
    enabled: !!jobId,
  });
}

export function useUpdateDialogueLine(jobId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      lineId,
      translatedText,
      characterId,
      safetyStatus,
    }: {
      lineId: string;
      translatedText?: string;
      characterId?: string;
      // Manual reviewer override — see
      // api/schemas/schemas.py::DialogueLineUpdate.safety_status for why
      // this exists: a "flagged" line never gets dubbed on its own after
      // just editing its text (DubbingAgent skips it regardless, and the
      // resume path deliberately doesn't re-run SafetyAgent) — this is the
      // only way to actually clear the flag and unblock dubbing.
      safetyStatus?: "ok" | "flagged";
    }) =>
      apiFetch<DialogueLine>(`/dialogue-lines/${lineId}`, {
        method: "PATCH",
        body: JSON.stringify({
          ...(translatedText !== undefined
            ? { translated_text: translatedText }
            : {}),
          ...(characterId !== undefined ? { character_id: characterId } : {}),
          ...(safetyStatus !== undefined
            ? { safety_status: safetyStatus }
            : {}),
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dialogue-lines", jobId] });
      // This PATCH can rewind job.current_stage to "safety_checked" and
      // republish it for an automatic redub+re-mux (see
      // update_dialogue_line's docstring in api/routers/jobs.py) — but the
      // response body here only returns the DialogueLine, never the Job
      // itself. Without this, the cached "job" query keeps showing the OLD
      // current_stage ("ready_for_review") until something else happens to
      // refetch it (e.g. the browser tab regaining focus, via TanStack
      // Query's default refetchOnWindowFocus) — found via code-reviewer:
      // useJob's refetchInterval only re-evaluates against CACHED data, so
      // without an explicit invalidation here it has no trigger to ever see
      // current_stage leave "ready_for_review" and start polling, meaning
      // the video-preview auto-refresh fix (see ReviewDashboard.tsx) would
      // silently never engage for a user who stays on this tab the whole
      // time — the exact scenario it's meant to fix.
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
}

export function useBatchUpdateDialogueLines(jobId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (
      lines: Array<{
        id: string;
        translated_text?: string;
        character_id?: string;
        safety_status?: "ok" | "flagged";
      }>,
    ) =>
      apiFetch<DialogueLine[]>(`/jobs/${jobId}/dialogue-lines/batch`, {
        method: "PATCH",
        body: JSON.stringify({ lines }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dialogue-lines", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["project-jobs"] });
    },
  });
}

export function useApproveJob(jobId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<Job>(`/jobs/${jobId}/approve`, { method: "POST" }),
    // ReviewDashboard.tsx doesn't fetch a job on its own (only dialogue-lines)
    // — old bug: invalidating the "job" key was correct, but no query on this
    // page actually used that key, so nothing happened after publishing
    // (manually reproduced: API returns 200 but the UI stays frozen).
    // "project-jobs" is the REAL query ProjectDetail.tsx uses to display job
    // status — invalidate it so that when the user goes back to that page
    // they see the latest "published" status.
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["project-jobs"] }),
  });
}
