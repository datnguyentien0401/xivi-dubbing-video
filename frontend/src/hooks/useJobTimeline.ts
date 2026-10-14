import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { TimelineState } from "@/stores/editorStore";

/** The job's current timeline (Video Editor, specs/002-video-editor) —
 * returns the saved snapshot, or a default snapshot the backend builds on the
 * fly from dialogue_lines if the editor has never been opened for this job
 * (see backend/api/routers/jobs.py::_build_default_timeline). Returns 409 if
 * the job isn't at the ready_for_review stage yet (still processing / still
 * re-dubbing a line) — `retry: false` so it doesn't keep retrying an error
 * that won't resolve itself after a few seconds.
 *
 * `staleTime: Infinity` — fetches exactly once as the INITIAL snapshot for
 * the editor session, with no automatic background refetch (e.g. when the
 * user alt-tabs back to the window — TanStack Query refetches on refocus by
 * default). VideoEditor.tsx pushes every `timeline` change into editorStore
 * via `loadTimeline`, and `loadTimeline` WIPES the undo/redo stack + playhead
 * (see editorStore.ts) — if background refetch were left on, refocusing the
 * window mid-edit would silently wipe out in-progress undo/redo history
 * (found during review, before real US5/autosave was built — blocked at the
 * root now instead of leaving it as debt). After the first fetch, the
 * session's source of truth is editorStore (autosave writes back to the
 * server via a separate PATCH, see useAutosaveTimeline), not this query
 * anymore. */
export function useJobTimeline(jobId: string | undefined) {
  return useQuery({
    queryKey: ["job-timeline", jobId],
    queryFn: () => apiFetch<TimelineState>(`/jobs/${jobId}/timeline`),
    enabled: !!jobId,
    retry: false,
    staleTime: Infinity,
  });
}

const AUTOSAVE_DEBOUNCE_MS = 1500;

/** Debounced autosave for the editor — call `scheduleAutosave(timeline)`
 * every time the user makes an edit (drag/cut/change volume...), and it only
 * actually PATCHes the server after AUTOSAVE_DEBOUNCE_MS has passed since the
 * last call, instead of firing a request for every tiny action. The client
 * always sends the FULL current timeline snapshot (not a diff), matching the
 * contract of `PATCH /jobs/{id}/timeline` (see
 * backend/api/routers/jobs.py::update_job_timeline). The debounce is a small
 * hand-rolled implementation (setTimeout/clearTimeout), no extra library
 * added (see plan.md).
 *
 * Does NOT handle the race between two near-simultaneous PATCH calls
 * overwriting each other in the cache (an older response arriving later can
 * overwrite a newer one) — this is acceptable, the same lost-update risk
 * already accepted on the backend side for the MVP (see the
 * `update_job_timeline` docstring: it assumes only one person edits a given
 * job at a time).
 *
 * Uses `mutateRef` (instead of closing over `mutation.mutate` directly inside
 * the setTimeout) so the timer callback always calls the LATEST version of
 * the mutate function — avoiding a stale closure if the component re-renders
 * while the debounce is pending (even though in practice TanStack Query's
 * `mutate` is fairly stable, using a ref still eliminates this risk entirely
 * instead of relying on the library's internal details). */
export function useAutosaveTimeline(jobId: string | undefined) {
  const queryClient = useQueryClient();
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Holds the LATEST snapshot not yet sent — needed to "flush" during the
  // cleanup below (a plain clearTimeout only cancels, it doesn't send the
  // scheduled payload).
  const pendingTimelineRef = useRef<TimelineState | null>(null);
  // true from the moment scheduleAutosave() is called (waiting out the
  // debounce) until the debounce elapses and the request is actually sent —
  // combined with `mutation.isPending` (true while the request is in flight)
  // to produce the correct `isSaving`, covering BOTH the "not yet sent" and
  // "sending" phases. The caller (VideoEditor.tsx) does not infer this from
  // local state (hasEdited / comparing timelines) — comparing the
  // `storeTimeline !== timeline` reference was tried and found WRONG when
  // tested in the browser: the two objects are NEVER the same reference, even
  // after a successful PATCH (the response is a freshly parsed JSON object,
  // not the same instance as the timeline that was sent), so that approach
  // got stuck on "Saving…" forever after the first edit — switched to
  // exposing the saving state directly from THIS hook to be certain it's
  // correct.
  const [isScheduled, setIsScheduled] = useState(false);

  const mutation = useMutation({
    mutationFn: (timeline: TimelineState) =>
      apiFetch<TimelineState>(`/jobs/${jobId}/timeline`, {
        method: "PATCH",
        body: JSON.stringify({ timeline }),
      }),
    onSuccess: (saved) => {
      // setQueryData instead of invalidateQueries — the PATCH response IS
      // ALREADY the just-saved snapshot, so refetching via GET right after
      // would only waste an extra round-trip.
      queryClient.setQueryData(["job-timeline", jobId], saved);
    },
  });

  const mutateRef = useRef(mutation.mutate);
  mutateRef.current = mutation.mutate;

  useEffect(() => {
    // Changing jobs or unmounting while a debounce is still pending — the
    // pending snapshot MUST be sent immediately (flushed) rather than just
    // cancelling the timer, otherwise the user's LAST edit (e.g. just
    // finished dragging a clip then left the editor within
    // AUTOSAVE_DEBOUNCE_MS) would be LOST ENTIRELY, never reaching the
    // server — found during review (`clearTimeout` doesn't send the
    // scheduled payload on its own).
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        if (pendingTimelineRef.current) {
          mutateRef.current(pendingTimelineRef.current);
          pendingTimelineRef.current = null;
        }
      }
    };
  }, [jobId]);

  function scheduleAutosave(timeline: TimelineState) {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    pendingTimelineRef.current = timeline;
    setIsScheduled(true);
    timeoutRef.current = setTimeout(() => {
      pendingTimelineRef.current = null;
      setIsScheduled(false);
      mutateRef.current(timeline);
    }, AUTOSAVE_DEBOUNCE_MS);
  }

  return {
    scheduleAutosave,
    isSaving: isScheduled || mutation.isPending,
    isError: mutation.isError,
    error: mutation.error,
  };
}
