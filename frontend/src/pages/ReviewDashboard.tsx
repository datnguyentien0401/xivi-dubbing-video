import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  Download,
  Film,
  Mic,
  RefreshCw,
  Save,
  Scissors,
} from "lucide-react";
import { AudioPlayer } from "@/components/AudioPlayer";
import { SafetyBadge } from "@/components/SafetyBadge";
import { ShareVideoPanel } from "@/components/ShareVideoPanel";
import {
  useApproveJob,
  useAttachVideo,
  useBatchUpdateDialogueLines,
  useDialogueLines,
  useDownloadDubbedAudioBundle,
  useJob,
  useJobCharacters,
  useJobVideoUrl,
  useUpdateDialogueLine,
  type DialogueLine,
} from "@/hooks/useDialogueLines";
import { useCreateUploadUrl } from "@/hooks/useProjects";
import { useJobStatus } from "@/hooks/useJobStatus";
import type { Character } from "@/hooks/useCharacters";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

// Radix SelectItem forbids an empty-string value (reserved internally for
// "no selection") — this sentinel stands in for "— Unassigned —" only
// inside this component; onSave always receives the real "" the rest of
// the app expects (see CharacterVoiceSelect below).
const UNASSIGNED_VALUE = "__unassigned__";

function CharacterVoiceSelect({
  line,
  characters,
  onSave,
  disabled,
}: {
  line: DialogueLine;
  characters: Character[];
  onSave: (characterId: string) => void;
  disabled: boolean;
}) {
  const initial = line.character_id ?? "";
  const [draft, setDraft] = useState(initial);
  const isDirty = draft !== "" && draft !== initial;

  return (
    <div className="flex items-center gap-2">
      <Select
        value={draft === "" ? UNASSIGNED_VALUE : draft}
        onValueChange={(value) =>
          setDraft(value === UNASSIGNED_VALUE ? "" : value)
        }
        disabled={disabled}
      >
        <SelectTrigger className="h-8 text-sm">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={UNASSIGNED_VALUE}>— Unassigned —</SelectItem>
          {characters.map((character) => (
            <SelectItem key={character.id} value={character.id}>
              {character.name}
              {character.voice_name ? ` (${character.voice_name})` : ""}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {isDirty && (
        <Button
          size="sm"
          className="h-8 shrink-0"
          onClick={() => onSave(draft)}
          disabled={disabled}
        >
          <RefreshCw size={12} />
          Re-dub
        </Button>
      )}
    </div>
  );
}

function EditableTranslation({
  line,
  value,
  onChange,
  onSave,
  disabled,
}: {
  line: DialogueLine;
  value: string;
  onChange: (text: string) => void;
  onSave: (text: string) => void;
  disabled: boolean;
}) {
  const isDirty = value !== (line.translated_text ?? "");

  return (
    <div className="flex items-center gap-2">
      <Input
        className={cn(
          "h-8 text-sm",
          isDirty && "border-primary/80 bg-primary/5 focus-visible:ring-primary",
        )}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      />
      {isDirty && (
        <Button
          size="sm"
          className="h-8 shrink-0"
          onClick={() => onSave(value)}
          disabled={disabled}
          title="Save this line"
        >
          <Save size={12} />
          Save
        </Button>
      )}
    </div>
  );
}

/** Manual reviewer override for safety_status — real UX gap found via
 * feedback: a "Needs Review" (flagged) line never gets dubbed on its own
 * after fixing its translated text (DubbingAgent skips any line that isn't
 * safety_status === "ok" regardless, see dubbing_agent.py, and the backend's
 * resume path deliberately doesn't re-run SafetyAgent on a text edit — see
 * api/routers/jobs.py::update_dialogue_line) — this is the only way to
 * actually clear the flag and let it get dubbed. Saving "ok" here (after the
 * text has already been fixed) is what triggers the re-dub; saving "flagged"
 * revokes any existing audio for a line the reviewer decides is unsafe. */
function EditableSafetyStatus({
  line,
  onSave,
  disabled,
}: {
  line: DialogueLine;
  onSave: (status: "ok" | "flagged") => void;
  disabled: boolean;
}) {
  // "pending" (SafetyAgent hasn't finished checking yet) has no edit target
  // of its own — normalize it to "ok" as the default draft/baseline so the
  // Save button doesn't appear spuriously before the user touches anything.
  // In practice every line reaching Review has already been checked, so
  // "pending" here would only mean the AI check is still running.
  const normalized: "ok" | "flagged" =
    line.safety_status === "flagged" ? "flagged" : "ok";
  const [draft, setDraft] = useState<"ok" | "flagged">(normalized);
  const isDirty = draft !== normalized;

  return (
    <div className="flex flex-col items-start gap-1.5">
      <SafetyBadge
        status={line.safety_status}
        reasoning={line.safety_reasoning}
      />
      <div className="flex items-center gap-2">
        <Select
          value={draft}
          onValueChange={(value) => setDraft(value as "ok" | "flagged")}
          disabled={disabled}
        >
          <SelectTrigger className="h-7 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="ok">Safe</SelectItem>
            <SelectItem value="flagged">Needs Review</SelectItem>
          </SelectContent>
        </Select>
        {isDirty && (
          <Button
            size="sm"
            className="h-7 shrink-0"
            onClick={() => onSave(draft)}
            disabled={disabled}
          >
            <Save size={12} />
            Save
          </Button>
        )}
      </div>
    </div>
  );
}

/** Lets the creator attach a real video to an already-dubbed script-first
 * job (specs/003-script-first-ingestion, US5) — uploads the video the same
 * 2-step signed-URL way NewProject.tsx does, then links it via
 * POST /jobs/{id}/attach-video. A plain file input styled to look like a
 * Button (`buttonVariants`) since it needs to sit inline in the header next
 * to the other actions but isn't itself a <button> element. */
function AttachVideoButton({
  jobId,
  projectId,
}: {
  jobId: string;
  projectId: string;
}) {
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const createUploadUrl = useCreateUploadUrl();
  const attachVideo = useAttachVideo(jobId);

  async function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    setIsUploading(true);
    try {
      const { upload_url, source_video_id } = await createUploadUrl.mutateAsync(
        {
          projectId,
          filename: file.name,
          content_type: file.type || "video/mp4",
        },
      );
      const putResponse = await fetch(upload_url, {
        method: "PUT",
        headers: { "Content-Type": file.type || "video/mp4" },
        body: file,
      });
      if (!putResponse.ok) {
        throw new Error(`Video upload to GCS failed (${putResponse.status})`);
      }
      await attachVideo.mutateAsync({ source_video_id });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not attach video.");
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <div>
      <label
        className={cn(
          buttonVariants({ variant: "outline" }),
          isUploading ? "cursor-not-allowed opacity-50" : "cursor-pointer",
        )}
      >
        <Film size={16} />
        {isUploading ? "Attaching video..." : "Attach Video"}
        <input
          type="file"
          accept="video/*"
          className="hidden"
          onChange={handleFileChange}
          disabled={isUploading}
        />
      </label>
      {error && <p className="mt-1 text-sm text-destructive">{error}</p>}
    </div>
  );
}

/** FR-008 (specs/003-script-first-ingestion) — lets a script-first job's
 * editor download every dubbed line's audio (named by segment_code) to cut
 * the real video against, before any video has been attached. Same
 * fetch-signed-URL-then-navigate pattern as ShareVideoPanel.tsx's own
 * handleDownload. */
function DownloadAudioBundleButton({ jobId }: { jobId: string }) {
  const downloadBundle = useDownloadDubbedAudioBundle(jobId);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setError(null);
    try {
      const result = await downloadBundle.mutateAsync();
      const a = document.createElement("a");
      a.href = result.url;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not download audio.",
      );
    }
  }

  return (
    <div>
      <Button
        variant="outline"
        onClick={handleClick}
        disabled={downloadBundle.isPending}
      >
        <Download size={16} />
        {downloadBundle.isPending ? "Preparing..." : "Download dubbed audio"}
      </Button>
      {error && <p className="mt-1 text-sm text-destructive">{error}</p>}
    </div>
  );
}

// see docs/frontend-spec.md section 3.4 — timestamp-synced video preview is
// still a TODO; the audio player + edit + approve parts are already in this
// version.
export default function ReviewDashboard() {
  const { jobId } = useParams<{ jobId: string }>();
  const { data: lines, isLoading } = useDialogueLines(jobId);
  const { data: characters } = useJobCharacters(jobId);
  // Polls while current_stage is away from its resting state so this
  // component learns when an in-progress "Attach Video" OR an
  // edit-triggered redub+re-mux finishes — see the comment on useJob itself
  // for why this polls directly rather than going through useJobStatus
  // (Firestore, with a REST-polling fallback in local dev): that fallback's
  // fixed 3s sampling can miss a fast muxing→ready_for_review round trip
  // (e.g. an immediate failure) entirely, since the whole transition can
  // complete between two samples with no OBSERVED change to react to.
  const { data: job } = useJob(jobId);
  const updateLine = useUpdateDialogueLine(jobId);
  const batchUpdate = useBatchUpdateDialogueLines(jobId);
  const approveJob = useApproveJob(jobId);
  const queryClient = useQueryClient();

  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const getDraft = (line: DialogueLine) =>
    drafts[line.id] !== undefined ? drafts[line.id] : (line.translated_text ?? "");

  const handleDraftChange = (lineId: string, text: string) => {
    setDrafts((prev) => ({ ...prev, [lineId]: text }));
  };

  const dirtyLines = (lines ?? []).filter((line) => {
    const draft = drafts[line.id];
    return draft !== undefined && draft !== (line.translated_text ?? "");
  });
  const dirtyCount = dirtyLines.length;

  async function handleSaveAll() {
    if (dirtyCount === 0 || editingLocked) return;
    const payload = dirtyLines.map((line) => ({
      id: line.id,
      translated_text: drafts[line.id],
    }));
    await batchUpdate.mutateAsync(payload);
    setDrafts({});
  }

  function handleDiscardAll() {
    setDrafts({});
  }
  // The video preview (useJobVideoUrl below) is its own independent query,
  // not reactively tied to `job` — so when muxed_video_gcs_uri changes to a
  // FRESH blob (every mux gets a brand new GCS path, see mux_service.py),
  // nothing tells the preview a new version exists on its own. Most
  // visibly: editing a dialogue line's text/character triggers an automatic
  // redub + re-mux (see update_dialogue_line's docstring in
  // api/routers/jobs.py) — the backend genuinely regenerates the video, but
  // without this, the on-screen preview kept showing the stale one until a
  // manual reload (found via user feedback). `prev === undefined` is the
  // "haven't seen a real job yet" sentinel — skips invalidating on the
  // very first render once `job` loads, only reacting to a REAL change
  // observed during this session (e.g. via the polling above).
  const previousMuxedUriRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!job) return;
    const prev = previousMuxedUriRef.current;
    previousMuxedUriRef.current = job.muxed_video_gcs_uri;
    if (prev !== undefined && prev !== job.muxed_video_gcs_uri) {
      queryClient.invalidateQueries({ queryKey: ["job-video-url", jobId] });
    }
  }, [job, jobId, queryClient]);
  // Firestore-only fields (muxSucceeded/durationWarning, US5) that GET
  // /jobs/{id} doesn't carry — used ONLY to show a specific "why it failed"
  // banner once useJob above (the authoritative, reliably-polled source for
  // current_stage/muxed_video_gcs_uri) already shows muxing has stopped.
  // Degrades to showing no extra detail in local dev (no Firestore there,
  // see useJobStatus.ts), same as exportProgress already does.
  const jobStatus = useJobStatus(jobId);

  // null (not just missing/undefined) means a script-first job with no
  // video attached yet (specs/003-script-first-ingestion) — see
  // backend/api/schemas/schemas.py::JobOut.source_video_id. Used ONLY for
  // the approve-button branch below, which mirrors the backend's OWN
  // source_video_id-based branching in approve_job — NOT a proxy for
  // "finished muxing" (see hasMuxedVideo, a materially different thing: a
  // failed mux leaves source_video_id set but muxed_video_gcs_uri empty).
  const hasVideo = !!job?.source_video_id;
  // Whether a real muxed video actually exists — derived directly from
  // muxed_video_gcs_uri, NOT from current_stage === "ready_for_review" (a
  // prior version did this and had a real bug: current_stage returns to
  // "ready_for_review" on a FAILED mux too, see worker/main.py::_attach_video's
  // finally block — using current_stage alone made "Open Editor" appear and
  // triggered a permanent GET /jobs/{id}/video-url 404 loop after a failure,
  // with no way to retry since the old "Attach Video" visibility check was
  // based on hasVideo, which also stays true after a failed attach).
  const hasMuxedVideo = !!job?.muxed_video_gcs_uri;
  const { data: video } = useJobVideoUrl(jobId, hasMuxedVideo);
  // A video can be (re-)attached whenever there's no successfully muxed
  // video yet AND the job is fully dubbed — this naturally also covers
  // retrying after a failed attach (source_video_id stays set from the
  // failed attempt, but muxed_video_gcs_uri stays empty, so this stays
  // true), which the backend now explicitly allows (see
  // api/routers/jobs.py::attach_video).
  const canAttachVideo =
    !hasMuxedVideo && job?.current_stage === "ready_for_review";

  const flaggedCount =
    lines?.filter((l) => l.safety_status === "flagged").length ?? 0;
  // job?.status handles the case where the user RELOADS the review page
  // after having already approved in a previous session (approveJob.isSuccess
  // is only true within the same session right after clicking the button,
  // and is lost immediately after reload — see useJob in
  // useDialogueLines.ts).
  const isPublished = job?.status === "published" || approveJob.isSuccess;
  // The editor can be opened while the job is ready_for_review — EVEN after
  // it has been published (current_stage doesn't change on publish, only
  // status does, see Clarifications #3 in specs/002-video-editor/spec.md).
  // The backend returns 409 on its own if the job is still processing /
  // re-dubbing a line / mid-export. Also requires hasMuxedVideo — a job with
  // no successfully muxed video has nothing for the editor to open
  // (specs/003-script-first-ingestion).
  const canOpenEditor = hasMuxedVideo;

  // Real bug found via user feedback: editing a SECOND line (or the same
  // line again) while an earlier edit's automatic redub+re-mux is still
  // running used to fail with a confusing 409 ("Job hasn't reached the
  // review stage yet") — update_dialogue_line requires
  // current_stage === "ready_for_review" (see api/routers/jobs.py), but
  // nothing stopped the user from attempting another edit during that
  // window, which for a redub-resume can last several seconds (reproduced
  // live: a 2nd edit sent just ~0.3s after the 1st already hit the 409).
  // Locking every editable control job-wide while current_stage is
  // anywhere else closes the window entirely, converting a confusing
  // backend error into a clear "please wait" state — consistent with how
  // AttachVideoButton/DownloadAudioBundleButton already disable themselves
  // during their own in-flight async work.
  const isReadyForReview = job?.current_stage === "ready_for_review";
  const editingLocked = !!job && !isReadyForReview;
  // "muxing" (Attach Video) and "exporting" (Video Editor export, reachable
  // here if the user clicks "Back to Review" before an export finishes)
  // each get their own dedicated status text below — this covers the
  // REMAINING transient stages, the ones a redub-resume actually passes
  // through (safety_checked/dubbed/muxed), so none of these messages ever
  // show at the same time. Found via code-reviewer: an earlier version of
  // this condition only excluded "muxing", so an in-progress export would
  // have been mislabeled "Processing previous edit..." instead of
  // "Exporting video...".
  const isRedubbing =
    editingLocked &&
    job?.current_stage !== "muxing" &&
    job?.current_stage !== "exporting";

  // Mirrors the backend's own 2 branches in approve_job (api/routers/jobs.py):
  // a video-first job only cares about flagged lines being resolved (the
  // muxed video already exists by the time current_stage reaches
  // ready_for_review); an audio-only job has no muxed video to fall back
  // on, so every "ok" line must actually have been dubbed, and at least one
  // line must have audio at all.
  const linesStillNeedingDub =
    lines?.filter((l) => l.safety_status === "ok" && !l.dubbed_audio_gcs_uri)
      .length ?? 0;
  const hasAnyDubbedLine = lines?.some((l) => l.dubbed_audio_gcs_uri) ?? false;
  let approveBlockedReason: string | undefined;
  // Checked FIRST, mirroring approve_job's own current_stage != "ready_for_review"
  // guard (api/routers/jobs.py) — found via code-reviewer: that guard used
  // to only check for "exporting", so approving during an edit-triggered
  // redub+re-mux would silently publish the STALE pre-edit video with no
  // error at all (the reviewer believes they approved the edited version,
  // but the published artifact doesn't reflect it). Blocking here too,
  // not just relying on the backend 409, gives a clear reason instead of a
  // failed-request banner.
  if (editingLocked) {
    approveBlockedReason = "Please wait for the current processing to finish";
  } else if (hasVideo) {
    if (flaggedCount > 0) {
      approveBlockedReason = "There are flagged lines that need to be resolved";
    }
  } else if (linesStillNeedingDub > 0) {
    approveBlockedReason = "Some lines are still being dubbed";
  } else if (!hasAnyDubbedLine) {
    approveBlockedReason = "No dialogue line has been dubbed yet";
  }

  if (isLoading) return <p className="p-6 text-muted-foreground">Loading...</p>;

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          {job?.project_id && (
            <Link
              to={`/projects/${job.project_id}`}
              className="mb-1 flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
            >
              <ArrowLeft size={14} />
              Back to Project
            </Link>
          )}
          <h1 className="font-display text-2xl font-semibold">
            Review Dashboard
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {dirtyCount > 0 && (
            <div className="flex items-center gap-2 rounded-lg border border-primary/50 bg-primary/10 px-3 py-1 text-sm animate-in fade-in">
              <span className="font-medium text-foreground">
                {dirtyCount} line{dirtyCount > 1 ? "s" : ""} modified
              </span>
              <Button
                size="sm"
                variant="default"
                onClick={handleSaveAll}
                disabled={editingLocked || batchUpdate.isPending}
                className="h-8 shadow-sm"
              >
                <Save size={14} className="mr-1.5" />
                {batchUpdate.isPending ? "Re-dubbing..." : `Save All Translated Script (${dirtyCount})`}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={handleDiscardAll}
                disabled={batchUpdate.isPending}
                className="h-8 text-xs text-muted-foreground hover:text-foreground"
              >
                Discard
              </Button>
            </div>
          )}
          {!hasMuxedVideo && hasAnyDubbedLine && jobId && (
            <DownloadAudioBundleButton jobId={jobId} />
          )}
          {canAttachVideo && jobId && job?.project_id && (
            <AttachVideoButton jobId={jobId} projectId={job.project_id} />
          )}
          {job?.current_stage === "muxing" && (
            <p className="text-sm text-muted-foreground">Attaching video...</p>
          )}
          {job?.current_stage === "exporting" && (
            <p className="text-sm text-muted-foreground">Exporting video...</p>
          )}
          {isRedubbing && (
            <p className="text-sm text-muted-foreground">
              Processing previous edit...
            </p>
          )}
          {job?.project_id && (
            <Button asChild variant="outline">
              <Link to={`/projects/${job.project_id}/voices`}>
                <Mic size={16} />
                Manage Voices
              </Link>
            </Button>
          )}
          {jobId && (
            <Button asChild variant="outline">
              <Link to={`/jobs/${jobId}/educational`}>
                <BookOpen size={16} />
                Educational Content
              </Link>
            </Button>
          )}
          {canOpenEditor && (
            <Button asChild variant="outline">
              <Link to={`/jobs/${jobId}/editor`}>
                <Scissors size={16} />
                Open Editor
              </Link>
            </Button>
          )}
          {isPublished ? (
            <Button asChild variant="success">
              <Link to="/studio">
                <CheckCircle2 size={16} />
                Published — Back to Dashboard
              </Link>
            </Button>
          ) : (
            <Button
              variant="success"
              disabled={!!approveBlockedReason || approveJob.isPending}
              onClick={() => approveJob.mutate()}
              title={approveBlockedReason}
            >
              <CheckCircle2 size={16} />
              {approveJob.isPending ? "Approving..." : "Approve & Publish"}
            </Button>
          )}
        </div>
      </div>

      {approveJob.isError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>
            Publish failed:{" "}
            {approveJob.error instanceof Error
              ? approveJob.error.message
              : "Unknown error"}
          </AlertDescription>
        </Alert>
      )}

      {updateLine.isError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>
            Edit/re-dub failed:{" "}
            {updateLine.error instanceof Error
              ? updateLine.error.message
              : "Unknown error"}
          </AlertDescription>
        </Alert>
      )}

      {batchUpdate.isError && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>
            Batch save/re-dub failed:{" "}
            {batchUpdate.error instanceof Error
              ? batchUpdate.error.message
              : "Unknown error"}
          </AlertDescription>
        </Alert>
      )}

      {jobStatus?.muxSucceeded === false && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle size={16} />
          <AlertDescription>
            Attaching the video failed while muxing — try attaching it again.
            {jobStatus.durationWarning && (
              <span className="block">{jobStatus.durationWarning}</span>
            )}
          </AlertDescription>
        </Alert>
      )}
      {jobStatus?.durationWarning && jobStatus.muxSucceeded !== false && (
        <Alert variant="warning" className="mb-4">
          <AlertTriangle size={16} />
          <AlertDescription>{jobStatus.durationWarning}</AlertDescription>
        </Alert>
      )}

      {isPublished && hasMuxedVideo && <ShareVideoPanel jobId={jobId} />}

      {video && (
        <video
          key={video.url}
          controls
          src={video.url}
          className="mb-6 max-h-[36rem] w-full rounded-lg border bg-black"
        />
      )}

      <Table className="table-fixed text-left">
        <colgroup>
          <col className="w-[18%]" />
          <col className="w-[34%]" />
          <col className="w-[16%]" />
          <col className="w-[18%]" />
          <col className="w-[14%]" />
        </colgroup>
        <TableHeader>
          <TableRow>
            <TableHead>Original</TableHead>
            <TableHead>
              <div className="flex items-center justify-between gap-2">
                <span>Translated Script</span>
                {dirtyCount > 0 && (
                  <Button
                    size="sm"
                    className="h-7 px-2 text-xs"
                    onClick={handleSaveAll}
                    disabled={editingLocked || batchUpdate.isPending}
                  >
                    <Save size={12} className="mr-1" />
                    Save All ({dirtyCount})
                  </Button>
                )}
              </div>
            </TableHead>
            <TableHead>Safety</TableHead>
            <TableHead>Character / Voice</TableHead>
            <TableHead>Audio</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines?.map((line) => (
            <TableRow key={line.id}>
              <TableCell className="break-words align-top">
                {line.original_text}
              </TableCell>
              <TableCell className="align-top">
                <EditableTranslation
                  line={line}
                  value={getDraft(line)}
                  onChange={(text) => handleDraftChange(line.id, text)}
                  onSave={(text) => {
                    updateLine.mutate({ lineId: line.id, translatedText: text });
                    setDrafts((prev) => {
                      const next = { ...prev };
                      delete next[line.id];
                      return next;
                    });
                  }}
                  disabled={editingLocked || batchUpdate.isPending}
                />
              </TableCell>
              <TableCell className="align-top">
                <EditableSafetyStatus
                  line={line}
                  onSave={(status) =>
                    updateLine.mutate({ lineId: line.id, safetyStatus: status })
                  }
                  disabled={editingLocked}
                />
              </TableCell>
              <TableCell className="align-top">
                <CharacterVoiceSelect
                  line={line}
                  characters={characters ?? []}
                  onSave={(characterId) =>
                    updateLine.mutate({ lineId: line.id, characterId })
                  }
                  disabled={editingLocked}
                />
              </TableCell>
              <TableCell className="align-top">
                {line.dubbed_audio_gcs_uri ? (
                  <AudioPlayer
                    gcsUri={line.dubbed_audio_gcs_uri}
                    signedUrlPath={`/dialogue-lines/${line.id}/audio-url`}
                  />
                ) : line.safety_status === "ok" && line.translated_text ? (
                  // Already meets the conditions to have audio (safe +
                  // translated) but doesn't yet — waiting for the worker to
                  // re-dub (e.g. just changed the voice or edited the text,
                  // see useUpdateDialogueLine), not the original "never
                  // dubbed" state.
                  <span className="text-muted-foreground">Re-dubbing...</span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
