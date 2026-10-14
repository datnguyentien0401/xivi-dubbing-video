import { Link, useNavigate, useParams } from "react-router-dom";
import { Activity, Eye, FileVideo, Mic, Plus, Trash2 } from "lucide-react";
import {
  useDeleteProject,
  useProjectJobs,
  useProjects,
  useSourceVideos,
} from "@/hooks/useProjects";
import type { Job } from "@/hooks/useDialogueLines";
import { EditableProjectName } from "@/components/EditableProjectName";
import { TARGET_LANGUAGE_BY_CODE } from "@/lib/targetLanguages";
import { Button } from "@/components/ui/button";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const STAGE_LABELS: Record<string, string> = {
  queued: "Queued",
  transcribed: "Transcribed",
  voice_profiled: "Voice Profiled",
  translated: "Translated",
  safety_checked: "Safety Checked",
  dubbed: "Dubbed",
  muxed: "Muxed",
  ready_for_review: "Ready for Review",
};

/** Falls back to the raw uppercase code for a language not in
 * TARGET_LANGUAGE_BY_CODE (defensive — e.g. a job created before a language
 * was added/renamed there) rather than rendering nothing. */
function languageDisplay(code: string): string {
  return TARGET_LANGUAGE_BY_CODE[code]?.englishLabel ?? code.toUpperCase();
}

function formatDuration(seconds: number | null): string | null {
  if (seconds === null) return null;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** A published job's current_stage stays frozen at "ready_for_review"
 * forever (publishing changes status, not stage — see
 * specs/002-video-editor/spec.md Clarifications #3), so "Published" is more
 * useful to show at a glance than the now-meaningless stage label. */
function jobStatusBadge(job: Job): {
  label: string;
  variant: BadgeProps["variant"];
} {
  if (job.status === "published")
    return { label: "Published", variant: "success" };
  if (job.current_stage === "ready_for_review") {
    return { label: "Ready for Review", variant: "warning" };
  }
  return {
    label: STAGE_LABELS[job.current_stage] ?? job.current_stage,
    variant: "secondary",
  };
}

/** 1 row in a job list — shared between the per-video sections and the
 * script-only section below (specs/003-script-first-ingestion), so a
 * script-first job (source_video_id === null) is just as trackable/
 * reviewable from this page as a video-first one. */
function JobRow({ job }: { job: Job }) {
  const badge = jobStatusBadge(job);
  const isReadyForReview = job.current_stage === "ready_for_review";

  return (
    <TableRow>
      <TableCell className="font-medium">
        {languageDisplay(job.target_language)}
      </TableCell>
      <TableCell>
        <Badge variant={badge.variant}>{badge.label}</Badge>
      </TableCell>
      <TableCell className="text-right">
        <Button asChild variant="ghost" size="sm">
          <Link
            to={
              isReadyForReview
                ? `/jobs/${job.id}/review`
                : `/jobs/${job.id}/status`
            }
          >
            {isReadyForReview ? <Eye size={14} /> : <Activity size={14} />}
            {isReadyForReview ? "View & Review" : "Track"}
          </Link>
        </Button>
      </TableCell>
    </TableRow>
  );
}

function JobTable({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No translations yet.</p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Language</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {jobs.map((job) => (
          <JobRow key={job.id} job={job} />
        ))}
      </TableBody>
    </Table>
  );
}

/** Previously there was no way to view the videos/jobs of a project that had
 * already been created — the Dashboard only had a button to create a new
 * project (see docs/frontend-spec.md, bug now fixed). This page lists every
 * video uploaded in the project plus the translation progress (job) for each
 * target language, linking to the correct status/review screen depending on
 * current_stage. */
export default function ProjectDetail() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { data: projects } = useProjects();
  const project = projects?.find((p) => p.id === projectId);
  const { data: videos, isLoading: loadingVideos } = useSourceVideos(projectId);
  const { data: jobs, isLoading: loadingJobs } = useProjectJobs(projectId);
  const deleteProject = useDeleteProject();
  // Script-first jobs (specs/003-script-first-ingestion) have no
  // source_video_id at all — without this, such a job would be permanently
  // invisible on this page once the user navigates away from the one-time
  // post-submit redirect to /jobs/{id}/status.
  const scriptJobs = jobs?.filter((j) => j.source_video_id === null) ?? [];

  if (loadingVideos || loadingJobs) {
    return <p className="p-6 text-muted-foreground">Loading...</p>;
  }

  function handleDelete() {
    if (!projectId) return;
    if (
      !window.confirm(
        `Delete project "${project?.name ?? ""}"? All videos, translations, and characters in this project will be removed from the system.`,
      )
    ) {
      return;
    }
    deleteProject.mutate(projectId, {
      onSuccess: () => navigate("/studio"),
    });
  }

  const isEmpty = (videos?.length ?? 0) === 0 && scriptJobs.length === 0;

  return (
    <div className="p-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          {project ? (
            <EditableProjectName project={project} headingLevel={1} />
          ) : (
            <h1 className="font-display text-2xl font-semibold">Project</h1>
          )}
          {project && (
            <Badge variant="outline" className="mt-2">
              {project.source_language.toUpperCase()}
            </Badge>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button asChild variant="outline">
            <Link to={`/projects/${projectId}/voices`}>
              <Mic size={14} />
              Manage Voices
            </Link>
          </Button>
          <Button asChild>
            <Link to={`/projects/${projectId}/new-video`}>
              <Plus size={14} />
              New Video
            </Link>
          </Button>
          <Button
            variant="destructiveOutline"
            onClick={handleDelete}
            disabled={deleteProject.isPending}
          >
            <Trash2 size={14} />
            Delete Project
          </Button>
        </div>
      </div>

      {isEmpty && (
        <Card className="flex flex-col items-center gap-2 border-dashed py-12 text-center">
          <FileVideo className="text-muted-foreground" size={28} />
          <p className="text-sm text-muted-foreground">
            No videos uploaded yet for this project.
          </p>
        </Card>
      )}

      <div className="space-y-4">
        {videos?.map((video) => {
          const videoJobs =
            jobs?.filter((j) => j.source_video_id === video.id) ?? [];
          const duration = formatDuration(video.duration_seconds);
          return (
            <Card key={video.id}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <FileVideo size={16} className="text-muted-foreground" />
                  {video.gcs_uri.split("/").pop()}
                </CardTitle>
                {duration && (
                  <CardDescription>Duration: {duration}</CardDescription>
                )}
              </CardHeader>
              <CardContent>
                <JobTable jobs={videoJobs} />
              </CardContent>
            </Card>
          );
        })}
      </div>

      {scriptJobs.length > 0 && (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle className="text-base">Script-only jobs</CardTitle>
            <CardDescription>No video attached yet</CardDescription>
          </CardHeader>
          <CardContent>
            <JobTable jobs={scriptJobs} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
