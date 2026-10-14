"""PUT/GET file endpoint for LocalStorageClient (local dev, see
worker/services/local_storage_client.py) — acting as a "fake GCS": PUT plays
the role of a signed upload URL (the frontend PUTs video/audio files directly
here), GET plays the role of playback (AudioPlayer.tsx, see gcsUriToPlayableUrl).

This router is ONLY included in the app when settings.use_local_storage=True
(see api/main.py) — it doesn't exist on the route table when running against
real GCS.

Doesn't go through get_current_user: a real GCS signed URL also doesn't pass
through the app's auth (it's signed directly by GCS, see
GCSClient.generate_signed_url) — this keeps the same security model in this
local fake version.
"""

import asyncio
import mimetypes
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

from api.core.config import settings

router = APIRouter(prefix="/local-storage", tags=["local-storage-dev-only"])

# "bytes=start-end" (end optional) — the only syntax real browsers actually
# send for <video>/<audio> (see RFC 7233 section 2.1); no need to support
# multi-range ("bytes=0-10,20-30") since no browser uses that form to seek video.
_RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")


def _safe_path(blob_name: str) -> Path:
    root = Path(settings.local_storage_dir).resolve()
    candidate = (root / blob_name).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid path")
    return candidate


@router.put("/{blob_name:path}")
async def put_object(blob_name: str, request: Request):
    dest = _safe_path(blob_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then os.replace() (atomic on the same filesystem)
    # when done — a client disconnecting mid-upload (large video) won't leave
    # a half-written file sitting at the FINAL blob path (the path the DB
    # already points to before the PUT actually runs, see
    # api/routers/projects.py:create_upload_url) — this avoids the worker
    # reading a file missing bytes without knowing it.
    tmp_path = dest.with_name(dest.name + ".part")
    try:
        # write() is blocking I/O — wrap it in to_thread so it doesn't block
        # the event loop (see AGENTS.md backend: all I/O must be async, same
        # convention as worker/services/gcs_client.py).
        with tmp_path.open("wb") as f:
            async for chunk in request.stream():
                await asyncio.to_thread(f.write, chunk)
        await asyncio.to_thread(os.replace, tmp_path, dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return {"status": "ok", "blob_name": blob_name}


@router.get("/{blob_name:path}")
async def get_object(blob_name: str, request: Request, download_filename: str | None = None):
    path = _safe_path(blob_name)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # Cache-Control: no-store — unlike a real GCS signed URL (which always has
    # its own expiration query param generated fresh each time, naturally
    # busting the browser cache), blob_name here is FIXED (e.g.
    # jobs/{job_id}/dubbed/{line_id}.wav) but the file's CONTENT can be
    # overwritten multiple times (re-dubbing a line). Without this header, the
    # browser might keep playing the OLD cached audio for that same URL even
    # though the file on disk has changed — this was observed for real: after
    # a redub finished, the backend generated the correct new audio (verified
    # by having Gemini transcribe the file on disk), but AudioPlayer still
    # played the old version.
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
    if download_filename:
        # Equivalent to a real GCS signed URL's response_disposition (see
        # GCSClient.generate_signed_read_url) — used for the Download button
        # in ReviewDashboard.tsx, forcing the browser to download instead of
        # opening/playing the file directly.
        headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'

    # Support HTTP Range — required for <video>/<audio> to be seekable. A
    # real GCS signed URL supports Range natively; Starlette's FileResponse
    # (the version in use, 0.38.6) does NOT — it returns the whole file with
    # 200 regardless of whether a Range header is present (manually
    # reproduced: clicking a clip in the Video Editor's Timeline.tsx to seek
    # doesn't move the playhead, video.seekable is always empty [[0,0]] even
    # though readyState is already HAVE_ENOUGH_DATA — due to this fake-GCS
    # endpoint lacking Range support, not a frontend bug). No need for
    # multi-range ("bytes=0-10,20-30") — no browser uses that form to seek
    # video. We DO handle suffix-range ("bytes=-N" = the last N bytes of the
    # file, RFC 7233 §2.1) even though it's rarer — some media players use
    # this form to read the trailing moov atom of a non-faststart MP4 file;
    # discovered during review that without handling it specially, the regex
    # still matches but MISINTERPRETS "bytes=-5" as start=0,end=5 (completely
    # wrong — it should mean "the last 5 bytes"), returning a 206 with WRONG
    # data instead of a clear error — worse than not supporting it at all.
    range_header = request.headers.get("range")
    if range_header:
        match = _RANGE_PATTERN.fullmatch(range_header.strip())
        if not match:
            raise HTTPException(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range header"
            )
        file_size = path.stat().st_size
        start_str, end_str = match.groups()
        if not start_str and not end_str:
            # "bytes=-" — missing both start and end, not a valid
            # suffix-range (a suffix-range MUST have a byte count after "-").
            raise HTTPException(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range header"
            )
        if not start_str:
            # suffix-range "bytes=-N": the last N bytes of the file, not
            # "from byte 0".
            suffix_length = int(end_str)
            if suffix_length == 0:
                raise HTTPException(
                    status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                    "Empty suffix-range is invalid",
                )
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
            end = min(end, file_size - 1)
        if file_size == 0 or start > end or start >= file_size:
            raise HTTPException(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "Range exceeds the file size"
            )
        chunk_length = end - start + 1

        def _read_range() -> bytes:
            with path.open("rb") as f:
                f.seek(start)
                return f.read(chunk_length)

        # to_thread — same sync-I/O convention used throughout this file (see put_object).
        body = await asyncio.to_thread(_read_range)
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(chunk_length)
        return Response(
            content=body,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers=headers,
        )

    return FileResponse(path, media_type=media_type, headers=headers)
