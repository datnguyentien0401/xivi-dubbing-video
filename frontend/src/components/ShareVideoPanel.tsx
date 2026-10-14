import { useState } from "react";
import { Check, Copy, Download, Share2 } from "lucide-react";
import { useDownloadJobVideo, useJobShareUrl } from "@/hooks/useDialogueLines";

function FacebookIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="#1877F2" aria-hidden="true">
      <path d="M22 12c0-5.52-4.48-10-10-10S2 6.48 2 12c0 4.84 3.44 8.87 8 9.8V15H8v-3h2V9.5C10 7.57 11.57 6 13.5 6H16v3h-2c-.55 0-1 .45-1 1v2h3v3h-3v6.95c5.05-.5 9-4.76 9-9.95z" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M18.9 2H22l-7.6 8.7L23 22h-6.9l-5.4-6.6L4.5 22H1.4l8.1-9.3L1 2h7l4.9 6 6-6zm-1.2 18h1.9L7.4 4H5.4l12.3 16z" />
    </svg>
  );
}

/** Panel for sharing/downloading the final video — only renders once the job
 * HAS been published (see ReviewDashboard.tsx, AGENTS.md principle #5).
 * Previously, after approval there was only a link back to the Dashboard,
 * with no way to get a link to or download the video outside the system. */
export function ShareVideoPanel({ jobId }: { jobId: string | undefined }) {
  const { data: share, isLoading } = useJobShareUrl(jobId, true);
  const downloadVideo = useDownloadJobVideo(jobId);
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!share?.url) return;
    await navigator.clipboard.writeText(share.url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  async function handleDownload() {
    const result = await downloadVideo.mutateAsync();
    // response_disposition=attachment is already baked into the URL on the
    // backend (see get_job_share_url), so simply navigating to this URL
    // makes the browser download it automatically — no need for the
    // `download` attribute (doesn't work cross-origin with a GCS signed URL).
    const a = document.createElement("a");
    a.href = result.url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  const canShareNatively = typeof navigator !== "undefined" && !!navigator.share;

  async function handleNativeShare() {
    if (!share?.url) return;
    try {
      await navigator.share({ url: share.url, title: "XIVI video" });
    } catch {
      // User cancelled the share sheet themselves — not an error, nothing to handle.
    }
  }

  const shareHref = share?.url ? encodeURIComponent(share.url) : null;

  return (
    <div className="mb-6 rounded border bg-gray-50 p-4">
      <p className="mb-2 text-sm font-semibold text-gray-700">Share this video</p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          readOnly
          value={isLoading ? "Generating link..." : (share?.url ?? "")}
          onFocus={(e) => e.target.select()}
          className="min-w-0 flex-1 rounded border bg-white px-2 py-1.5 text-sm text-gray-600"
        />
        <button
          type="button"
          onClick={handleCopy}
          disabled={!share?.url}
          className="flex items-center gap-1.5 rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-100 disabled:opacity-50"
        >
          {copied ? <Check size={14} className="text-green-600" /> : <Copy size={14} />}
          {copied ? "Copied" : "Copy Link"}
        </button>
        <button
          type="button"
          onClick={handleDownload}
          disabled={downloadVideo.isPending}
          className="flex items-center gap-1.5 rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-100 disabled:opacity-50"
        >
          <Download size={14} />
          {downloadVideo.isPending ? "Preparing..." : "Download"}
        </button>
        {canShareNatively && (
          <button
            type="button"
            onClick={handleNativeShare}
            disabled={!share?.url}
            className="flex items-center gap-1.5 rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-100 disabled:opacity-50"
          >
            <Share2 size={14} />
            Share
          </button>
        )}
        <a
          href={shareHref ? `https://www.facebook.com/sharer/sharer.php?u=${shareHref}` : undefined}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Share on Facebook"
          className={`flex items-center justify-center rounded border bg-white p-2 hover:bg-gray-100 ${!shareHref ? "pointer-events-none opacity-50" : ""}`}
        >
          <FacebookIcon />
        </a>
        <a
          href={shareHref ? `https://twitter.com/intent/tweet?url=${shareHref}` : undefined}
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Share on X"
          className={`flex items-center justify-center rounded border bg-white p-2 hover:bg-gray-100 ${!shareHref ? "pointer-events-none opacity-50" : ""}`}
        >
          <XIcon />
        </a>
      </div>
    </div>
  );
}
