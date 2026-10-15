import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Job } from "@/hooks/useDialogueLines";

export interface Project {
  id: string;
  name: string;
  source_language: string;
  created_at: string;
}

export interface SourceVideo {
  id: string;
  gcs_uri: string;
  duration_seconds: number | null;
  status: string;
}

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: () => apiFetch<Project[]>("/projects"),
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { name: string; source_language: string }) =>
      apiFetch<Project>("/projects", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

export function useUpdateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, name }: { projectId: string; name: string }) =>
      apiFetch<Project>(`/projects/${projectId}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) =>
      apiFetch<void>(`/projects/${projectId}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

/** Used by the ProjectDetail page (see
 * frontend/src/pages/ProjectDetail.tsx) — previously there was no way to
 * view the videos/jobs of a project that had already been created. */
export function useSourceVideos(projectId: string | undefined) {
  return useQuery({
    queryKey: ["source-videos", projectId],
    queryFn: () =>
      apiFetch<SourceVideo[]>(`/projects/${projectId}/source-videos`),
    enabled: !!projectId,
  });
}

/** Used by the per-video "Delete" button on ProjectDetail.tsx — removes 1
 * video + every job/dialogue-line tied to it (not the whole project, see
 * useDeleteProject above). Invalidates both queries since deleting a video
 * also deletes its jobs. */
export function useDeleteSourceVideo(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (videoId: string) =>
      apiFetch<void>(`/projects/${projectId}/source-videos/${videoId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["source-videos", projectId] });
      queryClient.invalidateQueries({ queryKey: ["project-jobs", projectId] });
    },
  });
}

export function useProjectJobs(projectId: string | undefined) {
  return useQuery({
    queryKey: ["project-jobs", projectId],
    queryFn: () => apiFetch<Job[]>(`/projects/${projectId}/jobs`),
    enabled: !!projectId,
  });
}

export interface UploadUrlResponse {
  upload_url: string;
  gcs_uri: string;
  source_video_id: string;
  expires_in_seconds: number;
}

/** Requests a signed URL from the backend then uploads directly to GCS — does
 * NOT push the file through the backend (avoids timeouts/bandwidth cost, see
 * docs/frontend-spec.md section 3.2). `projectId` is passed via the mutate
 * payload (not a hook parameter) because the project was just created in the
 * same submit flow — the component's `projectId` state hasn't re-rendered
 * yet by the time this next step is called. */
export function useCreateUploadUrl() {
  return useMutation({
    mutationFn: (payload: {
      projectId: string;
      filename: string;
      content_type: string;
    }) =>
      apiFetch<UploadUrlResponse>(`/projects/${payload.projectId}/upload-url`, {
        method: "POST",
        body: JSON.stringify({
          filename: payload.filename,
          content_type: payload.content_type,
        }),
      }),
  });
}
