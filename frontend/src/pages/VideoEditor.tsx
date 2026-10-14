import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download } from "lucide-react";
import {
  useDialogueLines,
  useJob,
  useJobCharacters,
  useJobVideoUrl,
  type Job,
} from "@/hooks/useDialogueLines";
import { useAutosaveTimeline, useJobTimeline } from "@/hooks/useJobTimeline";
import { useExportJob } from "@/hooks/useExportJob";
import { useJobStatus } from "@/hooks/useJobStatus";
import { useEditorStore, type Clip } from "@/stores/editorStore";
import { Timeline } from "@/components/editor/Timeline";
import { ClipInspector } from "@/components/editor/ClipInspector";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";

// Matches MIN_SPLIT_PIECE_SECONDS in editorStore.ts exactly (not exported
// separately since that's an internal detail of splitClip — the value is
// duplicated here ONLY to compute the disabled state for the Split button,
// it doesn't affect the actual split logic).
const MIN_SPLIT_GAP_SECONDS = 0.1;

/** Video Editor (specs/002-video-editor) — overall layout: video preview (on
 * top) + timeline (below, US1: view tracks/play-pause/seek/zoom; US2:
 * split/trim/delete+ripple video clips; US3: drag-and-drop/trim dialogue clip
 * edges + volume/mute/solo/fade — see Timeline.tsx, ClipBlock.tsx,
 * ClipInspector.tsx). Autosave (US5): every edit is debounce-written to the
 * server via `useAutosaveTimeline` (see useJobTimeline.ts) — there's no more
 * temporary local state, editorStore is the ONLY source of truth after the
 * first fetch, see the comment on `loadedJobIdRef` below for why it isn't
 * reloaded every time the query data changes. Export (US6): the Export button
 * publishes the job for the worker to render (`useExportJob`), with progress
 * + result tracked via Firestore realtime (`useJobStatus`, NOT polling — see
 * frontend/AGENTS.md); once export succeeds, the video preview is
 * automatically refetched so it can be previewed right away (the Approve &
 * Publish button stays in ReviewDashboard.tsx, not duplicated here). Can only
 * be opened while the job is
 * `ready_for_review` (the backend returns 409 otherwise, see GET
 * /jobs/{id}/timeline) — including after being published, since current_stage
 * doesn't change on publish (only status does, see Clarifications #3 in
 * spec.md). */
export default function VideoEditor() {
  const { jobId } = useParams<{ jobId: string }>();
  const { data: job } = useJob(jobId);
  const { data: timeline, isLoading, isError, error } = useJobTimeline(jobId);
  const { data: video } = useJobVideoUrl(jobId, true);
  const { data: characters } = useJobCharacters(jobId);
  const { data: dialogueLines } = useDialogueLines(jobId);
  const loadTimeline = useEditorStore((state) => state.loadTimeline);
  const setPlayhead = useEditorStore((state) => state.setPlayhead);
  const selectClip = useEditorStore((state) => state.selectClip);
  const selectedClipId = useEditorStore((state) => state.selectedClipId);
  const storeTimeline = useEditorStore((state) => state.timeline);
  const updateClip = useEditorStore((state) => state.updateClip);
  const splitClip = useEditorStore((state) => state.splitClip);
  const deleteClip = useEditorStore((state) => state.deleteClip);
  const playheadSeconds = useEditorStore((state) => state.playheadSeconds);
  const {
    scheduleAutosave,
    isSaving,
    isError: isSaveError,
  } = useAutosaveTimeline(jobId);
  const queryClient = useQueryClient();
  const jobStatus = useJobStatus(jobId);
  const exportMutation = useExportJob(jobId);

  const videoRef = useRef<HTMLVideoElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  // true as soon as the FIRST edit happens in the session — used to decide
  // whether to show the Saving/Saved indicator at all (before any edit
  // there's nothing to report a save status for, see the autosave useEffect
  // below).
  const [hasEdited, setHasEdited] = useState(false);
  // The result of the MOST RECENT export that has ACTUALLY completed (the
  // worker reports this via Firestore's currentStage leaving "exporting") —
  // null if no export has happened yet in this session, or an export has
  // finished and the user hasn't clicked Export again (hides the old
  // notification). See the useEffect watching jobStatus below.
  const [exportResult, setExportResult] = useState<"success" | "error" | null>(
    null,
  );
  // true from the moment currentStage="exporting" is FIRST SEEN (via
  // Firestore/polling fallback, see useJobStatus.ts) until it leaves that
  // state — needed to distinguish "never exported" from "just finished
  // exporting" when currentStage returns to "ready_for_review" (both states
  // share the same currentStage, differing only in whether "exporting" was
  // ever seen in between).
  const wasExportingRef = useRef(false);
  // The job's `exported_video_gcs_uri` captured right BEFORE the export
  // currently being tracked — used to tell success from failure once
  // currentStage leaves "exporting" (see the useEffect below). This does NOT
  // use Firestore's `exportProgress` for this purpose even though that field
  // exists (see useJobStatus.ts) — this local dev environment runs with
  // `USE_LOCAL_FIRESTORE=true`, and the worker DELIBERATELY skips all
  // Firestore writes (see worker/main.py::_update_export_status), so
  // `exportProgress` never has a value here even on a successful export —
  // using a signal from Postgres itself (exported_video_gcs_uri, via a plain
  // GET /jobs/{id}) keeps this correct in BOTH local dev and the cloud.
  const exportBaselineUriRef = useRef<string | null>(null);

  // The dialogue line corresponding to the currently SELECTED clip (if any)
  // — uses storeTimeline (not the raw `timeline` from the query) because
  // selectedClipId is a concept belonging to THIS SAME store; both are always
  // reset together by loadTimeline() when the job changes (see
  // editorStore.ts). NOTE (found during review): `dialogueLines` is a THIRD,
  // independent data source (its own react-query, not living in the store),
  // so there is a theoretical render frame where storeTimeline/selectedClipId
  // still belong to the OLD job while dialogueLines already belongs to the
  // NEW job — "safety" here comes from 2 layers: (1) dialogue line ids are
  // UUIDv4, so an accidental cross-job lookup almost never matches a real id
  // (it just falls back to null, never shows wrong content), and (2) the
  // loadTimeline() hand-off runs via useLayoutEffect (not useEffect) right
  // below — React re-renders synchronously BEFORE the browser paints a
  // frame, so the "mismatched" render frame above is never actually painted
  // to the screen. This is not "absolutely safe by design", just 2 layers of
  // protection that are good enough for this very low actual risk.
  const {
    selectedClip,
    selectedTrackId,
    selectedTrackType,
    selectedDialogueLine,
  } = useMemo(() => {
    if (!selectedClipId || !storeTimeline)
      return {
        selectedClip: null,
        selectedTrackId: null,
        selectedTrackType: null,
        selectedDialogueLine: null,
      };
    for (const track of storeTimeline.tracks) {
      const clip = track.clips.find((c) => c.id === selectedClipId);
      if (clip) {
        const line = clip.dialogue_line_id
          ? (dialogueLines?.find((l) => l.id === clip.dialogue_line_id) ?? null)
          : null;
        return {
          selectedClip: clip,
          selectedTrackId: track.id,
          selectedTrackType: track.type,
          selectedDialogueLine: line,
        };
      }
    }
    return {
      selectedClip: null,
      selectedTrackId: null,
      selectedTrackType: null,
      selectedDialogueLine: null,
    };
  }, [selectedClipId, storeTimeline, dialogueLines]);

  // US2: splitting is only valid when the playhead sits STRICTLY inside the
  // body of the selected video clip (editorStore.splitClip no-ops on its own
  // if too close to an edge, but disabling the button here is clearer than
  // letting the user click it and see nothing happen).
  const canSplitAtPlayhead = useMemo(() => {
    if (!selectedClip || selectedTrackType !== "video") return false;
    const clipEnd =
      selectedClip.timeline_start +
      (selectedClip.source_end - selectedClip.source_start);
    return (
      playheadSeconds > selectedClip.timeline_start + MIN_SPLIT_GAP_SECONDS &&
      playheadSeconds < clipEnd - MIN_SPLIT_GAP_SECONDS
    );
  }, [selectedClip, selectedTrackType, playheadSeconds]);

  const selectedCharacterName = useMemo(() => {
    if (!selectedDialogueLine?.character_id) return undefined;
    return characters?.find((c) => c.id === selectedDialogueLine.character_id)
      ?.name;
  }, [selectedDialogueLine, characters]);

  // Loads the fetched timeline into the store AS SOON AS it's available —
  // every downstream child component (Timeline/TrackRow/ClipBlock) only reads
  // from the store, never receiving it back via its own props/query (see
  // editorStore.ts). USES useLayoutEffect (not useEffect) — runs
  // synchronously before the browser paints a frame, so if loadTimeline()
  // needs to reset selectedClipId/storeTimeline for the new job, React
  // re-renders within the same commit cycle instead of letting a frame with
  // the old job's data slip onto the screen first (see the note on
  // selectedDialogueLine above).
  //
  // Loads ONLY ONCE per jobId (tracked via loadedJobIdRef), does NOT reload
  // every time `timeline` (the query data) changes — because after the first
  // successful autosave PATCH, useAutosaveTimeline's `onSuccess` calls
  // `setQueryData` to write the server's returned snapshot back into THIS
  // SAME query, which changes `timeline`'s reference even though the content
  // is identical to what was just saved; if it reloaded every time that
  // happened, loadTimeline() would wipe editorStore's undo/redo + selection +
  // playhead AFTER EVERY SUCCESSFUL AUTOSAVE — found while designing
  // autosave, blocked at the root rather than patched after hitting the bug
  // (exactly as the useJobTimeline.ts docstring warns: after the first
  // fetch, editorStore is the source of truth, not this query anymore).
  const loadedJobIdRef = useRef<string | undefined>(undefined);
  useLayoutEffect(() => {
    if (timeline && loadedJobIdRef.current !== jobId) {
      loadTimeline(timeline);
      loadedJobIdRef.current = jobId;
    }
  }, [timeline, jobId, loadTimeline]);

  // Escape deselects the current clip (closes ClipInspector) — real UX gap
  // found via feedback: once a clip was selected there was no way to close
  // the inspector panel besides selecting a DIFFERENT clip. Skips while
  // typing in an input/textarea (e.g. the fade in/out fields in
  // ClipInspector) so Escape there behaves as the browser/OS default
  // (cancel the edit, blur the field) instead of also closing the whole
  // panel out from under the user mid-edit.
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      const target = e.target as HTMLElement | null;
      if (target?.tagName === "INPUT" || target?.tagName === "TEXTAREA") return;
      selectClip(null);
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectClip]);

  // Autosave: every time storeTimeline's reference changes (= there's a new
  // edit, applyEdit always creates a new object — see editorStore.ts) AND it
  // differs from the version just fetched/saved, schedule a write to the
  // server. Deliberately does NOT include `timeline`/`scheduleAutosave` in
  // the deps — this effect should ONLY run when `storeTimeline` actually
  // changes, not every time VideoEditor re-renders for some other reason
  // (e.g. playheadSeconds updating continuously during video playback via
  // onTimeUpdate); if `scheduleAutosave` (a new function every render) were
  // added to the deps, the debounce timer would be CANCELLED AND RESET
  // continuously throughout playback, so autosave would never actually get a
  // chance to run.
  useEffect(() => {
    if (storeTimeline && storeTimeline !== timeline) {
      setHasEdited(true);
      scheduleAutosave(storeTimeline);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeTimeline]);

  // US6: detects when export has ACTUALLY finished (not when POST /export
  // was accepted — that's just publishing a message, the worker only changes
  // currentStage once ffmpeg finishes, see worker/main.py::_export_job).
  // Success/failure is determined by comparing `exported_video_gcs_uri` AFTER
  // currentStage leaves "exporting" against the value captured RIGHT BEFORE
  // the export started — the worker only sets this field on the SUCCESS
  // branch (see worker/main.py::_export_job,
  // `job.exported_video_gcs_uri = new_uri` sits BEFORE the `except`), so a
  // value different from the old one = success, unchanged = failure. Uses
  // `queryClient.invalidateQueries(...).then(...)` (rather than just setting
  // state and waiting on another effect watching `job`) because TanStack
  // Query v5 enables structural sharing by default — if the export FAILS,
  // the refetched data is identical to the old data, so `job` will NOT change
  // reference, meaning an effect watching `[job]` would never fire for the
  // failure branch; reading straight from the cache after the
  // invalidateQueries Promise resolves (meaning the refetch has REALLY
  // finished) is the only way to be correct on both branches.
  useEffect(() => {
    if (jobStatus?.currentStage === "exporting") {
      if (!wasExportingRef.current) {
        exportBaselineUriRef.current = job?.exported_video_gcs_uri ?? null;
      }
      wasExportingRef.current = true;
      setExportResult(null);
      return;
    }
    if (!wasExportingRef.current) return;
    wasExportingRef.current = false;
    queryClient.invalidateQueries({ queryKey: ["job", jobId] }).then(() => {
      const freshJob = queryClient.getQueryData<Job>(["job", jobId]);
      const succeeded =
        !!freshJob?.exported_video_gcs_uri &&
        freshJob.exported_video_gcs_uri !== exportBaselineUriRef.current;
      setExportResult(succeeded ? "success" : "error");
      if (succeeded) {
        // The video preview (US6: "preview the exported result") follows
        // _effective_video_gcs_uri, preferring the just-exported version —
        // refetch so <video> automatically switches to the new version
        // instead of continuing to show the old one.
        queryClient.invalidateQueries({ queryKey: ["job-video-url", jobId] });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobStatus?.currentStage, jobId, queryClient]);

  function handleExport() {
    setExportResult(null);
    exportMutation.mutate();
  }

  function handlePlayPause() {
    const videoEl = videoRef.current;
    if (!videoEl) return;
    if (videoEl.paused) videoEl.play();
    else videoEl.pause();
  }

  function handleSeek(seconds: number) {
    const videoEl = videoRef.current;
    // Update the store IMMEDIATELY (instant feedback for the playhead marker)
    // — the <video>'s timeupdate event afterwards just confirms this same
    // value, it doesn't cause a loop since setting the same value is
    // harmless.
    setPlayhead(seconds);
    if (videoEl) videoEl.currentTime = seconds;
  }

  function handleSplitSelectedClip() {
    if (!selectedClip || !selectedTrackId) return;
    splitClip(selectedTrackId, selectedClip.id, playheadSeconds);
  }

  // US2: "dialogue clips that fall within the cut segment are handled
  // according to the corresponding ripple mode (deleted/shifted together)" —
  // editorStore.deleteClip itself fully deletes any clip that falls ENTIRELY
  // within the cut segment, or SHORTENS a clip that only partially overlaps
  // it (see the comment on deleteClip in editorStore.ts). Warning before an
  // ALREADY-DUBBED dialogue line is affected (deleted OR shortened — the
  // degree isn't distinguished, kept simple for this MVP warning) has to be
  // computed here (needs both storeTimeline and dialogueLines at once to
  // know which lines actually have audio; the store doesn't hold
  // dialogueLines). "Dubbed" means having `dubbed_audio_gcs_uri`, not merely
  // having a `dialogue_line_id` (a line that hasn't been dubbed yet loses
  // nothing meaningful when deleted).
  function findOverlappingDubbedLines(clip: Clip) {
    if (!storeTimeline || !dialogueLines) return [];
    const cutStart = clip.timeline_start;
    const cutEnd = cutStart + (clip.source_end - clip.source_start);
    const overlappingLineIds = new Set<string>();
    for (const track of storeTimeline.tracks) {
      for (const c of track.clips) {
        const cStart = c.timeline_start;
        const cEnd = cStart + (c.source_end - c.source_start);
        if (c.dialogue_line_id && cStart < cutEnd && cEnd > cutStart) {
          overlappingLineIds.add(c.dialogue_line_id);
        }
      }
    }
    return dialogueLines.filter(
      (line) => overlappingLineIds.has(line.id) && line.dubbed_audio_gcs_uri,
    );
  }

  function handleDeleteSelectedClip(ripple: boolean) {
    if (!selectedClip || !selectedTrackId) return;
    const affected = findOverlappingDubbedLines(selectedClip);
    if (affected.length > 0) {
      const ok = window.confirm(
        `This will also remove or shorten ${affected.length} dubbed dialogue line${
          affected.length > 1 ? "s" : ""
        }. Continue?`,
      );
      if (!ok) return;
    }
    deleteClip(selectedTrackId, selectedClip.id, ripple);
  }

  const isExporting =
    jobStatus?.currentStage === "exporting" || exportMutation.isPending;
  // Blocks export while there are edits not yet saved —
  // /jobs/{id}/export reads timeline_edits DIRECTLY from Postgres (the latest
  // autosaved version); exporting while still debouncing/sending the PATCH
  // would build the video from a version OLDER than what the user just saw
  // on screen.
  const exportDisabled = isExporting || isSaving;

  if (isLoading)
    return <p className="p-6 text-muted-foreground">Loading editor...</p>;

  // A script-first job with no video attached yet
  // (specs/003-script-first-ingestion) has nothing for the editor to open —
  // ReviewDashboard.tsx already hides the "Open Editor" link for this case,
  // this is a defense-in-depth guard against reaching this page directly by
  // URL (GET /jobs/{id}/timeline itself degrades gracefully rather than
  // erroring for this case, building a nonsensical default timeline that
  // references a video that doesn't exist — so `isError` below would NOT
  // catch it on its own).
  if (job && !job.source_video_id) {
    return (
      <div className="p-6">
        <Link
          to={`/jobs/${jobId}/review`}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={16} />
          Back to Review
        </Link>
        <p className="mt-4 text-sm text-muted-foreground">
          This job has no video attached yet — attach one from the Review
          Dashboard before opening the editor.
        </p>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="p-6">
        <Link
          to={`/jobs/${jobId}/review`}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={16} />
          Back to Review
        </Link>
        <Alert variant="destructive" className="mt-4">
          <AlertDescription>
            Could not open editor:{" "}
            {error instanceof Error ? error.message : "Unknown error"}
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      <div className="flex shrink-0 items-center justify-between border-b bg-card p-4">
        <Link
          to={`/jobs/${jobId}/review`}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={16} />
          Back to Review
        </Link>
        <h1 className="font-display text-lg font-semibold">
          Video Editor{job ? ` — ${job.target_language}` : ""}
        </h1>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {hasEdited &&
              (isSaveError ? (
                <span className="text-destructive">Save failed</span>
              ) : isSaving ? (
                "Saving…"
              ) : (
                "Saved"
              ))}
          </span>
          <Button
            onClick={handleExport}
            disabled={exportDisabled}
            title={
              isSaving
                ? "Waiting for your latest edits to save first"
                : undefined
            }
          >
            <Download size={14} />
            {isExporting ? "Exporting…" : "Export"}
          </Button>
        </div>
      </div>
      {exportResult && (
        <div
          className={
            exportResult === "success"
              ? "shrink-0 bg-success/15 px-4 py-1.5 text-xs text-success"
              : "shrink-0 bg-destructive/15 px-4 py-1.5 text-xs text-destructive"
          }
        >
          {exportResult === "success"
            ? "Export complete — preview updated below."
            : "Export failed. Please try again."}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <div className="flex flex-1 items-center justify-center bg-black">
          {video && (
            <video
              ref={videoRef}
              src={video.url}
              className="max-h-full max-w-full"
              onTimeUpdate={(e) => setPlayhead(e.currentTarget.currentTime)}
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
            />
          )}
        </div>
        <ClipInspector
          dialogueLine={selectedDialogueLine}
          characterName={selectedCharacterName}
          clip={selectedClip}
          trackType={selectedTrackType}
          canSplitAtPlayhead={canSplitAtPlayhead}
          onUpdateClip={(patch) => {
            if (selectedTrackId && selectedClip)
              updateClip(selectedTrackId, selectedClip.id, patch);
          }}
          onSplitClip={handleSplitSelectedClip}
          onDeleteClip={handleDeleteSelectedClip}
          onClose={() => selectClip(null)}
        />
      </div>

      <div className="mb-4 mr-4 h-64 shrink-0 border-t bg-muted/50">
        <Timeline
          isPlaying={isPlaying}
          onPlayPause={handlePlayPause}
          onSeek={handleSeek}
          characters={characters ?? []}
          dialogueLines={dialogueLines ?? []}
        />
      </div>
    </div>
  );
}
