import { API_BASE_URL, apiFetch } from "@/lib/api";

/**
 * A gs://... / local:// URI cannot be played directly in the browser.
 *
 * - local://blob_name -> GET {API_BASE_URL}/local-storage/{blob_name} (local
 *   dev, see api/routers/local_storage.py) — no signing needed.
 * - gs://bucket/path -> MUST get a real signed URL from the backend
 *   (signedUrlPath) — the GCS bucket doesn't allow public read.
 *
 * Extracted out of AudioPlayer.tsx (its first use site) into here because
 * components/editor/WaveformClip.tsx (Video Editor, specs/002-video-editor)
 * needs this exact logic a second time — avoiding duplication instead of
 * copy-pasting.
 */
export async function resolvePlayableUrl(
  gcsUri: string,
  signedUrlPath?: string,
): Promise<string> {
  const localMatch = /^local:\/\/(.+)$/.exec(gcsUri);
  if (localMatch) {
    const [, blobName] = localMatch;
    // Encode each path segment separately (keeping "/" as the path
    // separator) — matching how the backend generates this URL
    // (urllib.parse.quote with the default safe="/", see
    // local_storage_client.py), avoiding reliance on whether ASGI decodes
    // %2F back to "/" before the "{blob_name:path}" route matches.
    const encodedPath = blobName.split("/").map(encodeURIComponent).join("/");
    return `${API_BASE_URL}/local-storage/${encodedPath}`;
  }
  if (!signedUrlPath) return gcsUri;
  const { url } = await apiFetch<{ url: string }>(signedUrlPath);
  return url;
}
