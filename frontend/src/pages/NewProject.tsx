import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Sparkles, UploadCloud } from "lucide-react";
import { useCreateJobs } from "@/hooks/useDialogueLines";
import {
  useCreateProject,
  useCreateUploadUrl,
  useProjects,
} from "@/hooks/useProjects";
import {
  SOURCE_LANGUAGES,
  TARGET_LANGUAGE_GROUPS,
} from "@/lib/targetLanguages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// New Project — upload video (signed URL, uploaded directly to GCS, not
// through the backend) + choose source/target languages, submit -> POST
// /jobs -> navigate to Pipeline Status (see docs/frontend-spec.md section
// 3.2).
//
// Also doubles as "Add Video" for an EXISTING project (route
// /projects/:projectId/new-video, linked from ProjectDetail.tsx's "New
// Video" button) — real UX bug found via feedback: that button used to
// point at this same /projects/new route with no way to carry the current
// project's id, so clicking "New Video" from inside a project actually
// created a brand new, unrelated project instead of adding a video to the
// one being viewed. When `projectId` is present, the Name/Source Language
// fields (which only make sense once, at project creation) are skipped
// entirely and the existing project is reused as-is.
export default function NewProject() {
  const { projectId: existingProjectId } = useParams<{ projectId?: string }>();
  const { data: projects } = useProjects();
  const existingProject = existingProjectId
    ? projects?.find((p) => p.id === existingProjectId)
    : undefined;
  const isAddingToExistingProject = !!existingProjectId;

  const [name, setName] = useState("");
  const [sourceLanguage, setSourceLanguage] = useState("en");
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [targetLanguages, setTargetLanguages] = useState<string[]>([]);
  const [step, setStep] = useState<
    "idle" | "creating-project" | "uploading" | "creating-jobs"
  >("idle");
  const [error, setError] = useState<string | null>(null);

  const createProject = useCreateProject();
  const createUploadUrl = useCreateUploadUrl();
  const createJobs = useCreateJobs();
  const navigate = useNavigate();

  const isSubmitting = step !== "idle";
  const canSubmit =
    (isAddingToExistingProject || name.trim() !== "") &&
    videoFile !== null &&
    targetLanguages.length > 0 &&
    !isSubmitting;

  function toggleTargetLanguage(code: string) {
    setTargetLanguages((prev) =>
      prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code],
    );
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!videoFile || targetLanguages.length === 0) return;
    setError(null);
    // Set step BEFORE the first await (not after) — otherwise, double-clicking
    // the submit button while POST /projects is in flight would trigger a
    // second, parallel run of handleSubmit, since canSubmit would still be
    // true throughout that network round-trip.
    setStep(isAddingToExistingProject ? "uploading" : "creating-project");

    try {
      const projectId = isAddingToExistingProject
        ? existingProjectId!
        : (
            await createProject.mutateAsync({
              name,
              source_language: sourceLanguage,
            })
          ).id;

      setStep("uploading");
      const { upload_url, source_video_id } = await createUploadUrl.mutateAsync(
        {
          projectId,
          filename: videoFile.name,
          content_type: videoFile.type || "video/mp4",
        },
      );
      const putResponse = await fetch(upload_url, {
        method: "PUT",
        headers: { "Content-Type": videoFile.type || "video/mp4" },
        body: videoFile,
      });
      if (!putResponse.ok) {
        throw new Error(`Video upload to GCS failed (${putResponse.status})`);
      }

      setStep("creating-jobs");
      const jobs = await createJobs.mutateAsync({
        source_video_id,
        target_languages: targetLanguages,
      });

      navigate(`/jobs/${jobs[0].id}/status`);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong, please try again.",
      );
      setStep("idle");
    }
  }

  return (
    <div className="p-6">
      <Card className="mx-auto max-w-xl">
        <CardHeader>
          <CardTitle className="text-2xl">
            {isAddingToExistingProject
              ? `Add Video${existingProject ? ` — ${existingProject.name}` : ""}`
              : "New Project"}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {!isAddingToExistingProject && (
              <>
                <div>
                  <Label htmlFor="project-name" className="mb-1.5 block">
                    Project Name
                  </Label>
                  <Input
                    id="project-name"
                    placeholder="e.g. Froggy - Episode 1"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    disabled={isSubmitting}
                  />
                </div>

                <div>
                  <Label className="mb-1.5 block">Source Video Language</Label>
                  <Select
                    value={sourceLanguage}
                    onValueChange={setSourceLanguage}
                    disabled={isSubmitting}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SOURCE_LANGUAGES.map((lang) => (
                        <SelectItem key={lang.code} value={lang.code}>
                          {lang.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}

            <div>
              <Label htmlFor="video-file" className="mb-1.5 block">
                Source Video
              </Label>
              <label
                htmlFor="video-file"
                className={`flex items-center gap-2 rounded-md border border-dashed px-3 py-3 text-sm transition-colors ${
                  isSubmitting
                    ? "cursor-not-allowed opacity-50"
                    : "cursor-pointer hover:border-primary hover:bg-accent/40"
                }`}
              >
                <UploadCloud
                  size={18}
                  className="shrink-0 text-muted-foreground"
                />
                <span className="truncate text-muted-foreground">
                  {videoFile ? videoFile.name : "Click to choose a video file"}
                </span>
                <input
                  id="video-file"
                  type="file"
                  accept="video/*"
                  className="hidden"
                  onChange={(e) => setVideoFile(e.target.files?.[0] ?? null)}
                  disabled={isSubmitting}
                />
              </label>
            </div>

            <div>
              <Label className="mb-1.5 block">
                Target Languages (select at least 1)
              </Label>
              <div className="space-y-3">
                {TARGET_LANGUAGE_GROUPS.map((group) => (
                  <div key={group.region}>
                    <p className="mb-1.5 text-xs font-semibold text-muted-foreground">
                      {group.region}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {group.languages.map((lang) => {
                        const checked = targetLanguages.includes(lang.code);
                        return (
                          <label
                            key={lang.code}
                            className={`flex cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm transition-colors ${
                              checked
                                ? "border-primary bg-accent text-accent-foreground"
                                : "hover:bg-accent/40"
                            }`}
                          >
                            <input
                              type="checkbox"
                              className="accent-[hsl(var(--primary))]"
                              checked={checked}
                              onChange={() => toggleTargetLanguage(lang.code)}
                              disabled={isSubmitting}
                            />
                            <span>{lang.flag}</span>
                            <span>{lang.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <Button type="submit" disabled={!canSubmit} className="w-full">
              <Sparkles size={16} />
              {step === "idle" &&
                (isAddingToExistingProject ? "Add Video" : "Create Project")}
              {step === "creating-project" && "Creating project..."}
              {step === "uploading" && "Uploading video..."}
              {step === "creating-jobs" && "Initializing pipeline..."}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
