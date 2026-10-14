import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

export interface Character {
  id: string;
  name: string;
  voice_sample_gcs_uri: string | null;
  /** Prebuilt voice (one of 12 fixed voices) — only used when there is no
   * voice_id (fallback). */
  voice_name: string | null;
  /** Custom voice synthesized via Voice Design, matching the original
   * voice's characteristics far better than voice_name — prefer this for
   * display when present. */
  voice_id: string | null;
}

export function useCharacters(projectId: string | undefined) {
  return useQuery({
    queryKey: ["characters", projectId],
    queryFn: () => apiFetch<Character[]>(`/characters?project_id=${projectId}`),
    enabled: !!projectId,
    // Light polling while any character still lacks voice_id/voice_name
    // (waiting on the worker to run Voice Design + async fallback after the
    // user uploads a sample — see useClassifyVoiceSample) — polling stops on
    // its own once every character either has a sample plus one of those two
    // fields, or has no sample at all (nothing left to wait for).
    refetchInterval: (query) => {
      const characters = query.state.data;
      const hasPending = characters?.some(
        (c) => c.voice_sample_gcs_uri && !c.voice_id && !c.voice_name,
      );
      return hasPending ? 3000 : false;
    },
  });
}

export function useUpdateCharacter(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ characterId, name }: { characterId: string; name: string }) =>
      apiFetch<Character>(`/characters/${characterId}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["characters", projectId] }),
  });
}

export function useCreateCharacter(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { name: string }) =>
      apiFetch<Character>("/characters", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, name: payload.name }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["characters", projectId] }),
  });
}

interface VoiceSampleUploadUrlResponse {
  upload_url: string;
  gcs_uri: string;
}

export function useCreateVoiceSampleUploadUrl() {
  return useMutation({
    mutationFn: (payload: {
      characterId: string;
      filename: string;
      content_type: string;
    }) =>
      apiFetch<VoiceSampleUploadUrlResponse>(
        `/characters/${payload.characterId}/voice-sample-upload-url`,
        {
          method: "POST",
          body: JSON.stringify({
            filename: payload.filename,
            content_type: payload.content_type,
          }),
        },
      ),
  });
}

/** Triggers the worker to listen to the just-uploaded sample and pick the
 * closest TTS voice (asynchronously) — call this AFTER the file has been PUT
 * to the upload_url from the step above. The result (voice_name) appears
 * gradually via useCharacters' polling, not returned immediately. */
export function useClassifyVoiceSample(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (characterId: string) =>
      apiFetch<Character>(`/characters/${characterId}/classify-voice-sample`, {
        method: "POST",
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["characters", projectId] }),
  });
}

export interface VoiceOption {
  voice_name: string;
  description: string;
}

/** The 12 prebuilt TTS voices, for a human to pick from directly — used by
 * VoicePicker. A fixed, global list (not per-project), so it's cached
 * indefinitely rather than refetched like project-scoped data. */
export function useVoiceOptions() {
  return useQuery({
    queryKey: ["voice-options"],
    queryFn: () => apiFetch<VoiceOption[]>("/characters/voice-options"),
    staleTime: Infinity,
  });
}

/** Manually assign a character's voice directly from the prebuilt list — no
 * audio sample, no Gemini classification call (contrast with
 * useClassifyVoiceSample above). Used when there's no original recording for
 * Gemini to listen to and match automatically (specs/003-script-first-ingestion). */
export function useAssignVoiceManually(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ characterId, voiceName }: { characterId: string; voiceName: string }) =>
      apiFetch<Character>(`/characters/${characterId}/voice`, {
        method: "PUT",
        body: JSON.stringify({ voice_name: voiceName }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["characters", projectId] }),
  });
}
