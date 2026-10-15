"""Drop-in replacement for GCSClient for local dev (docker-compose,
USE_LOCAL_STORAGE=true) — no real GCP service account needed. Stores files on
a volume shared between api and worker (see docker-compose.yml). URIs use the
`local://{blob_name}` scheme (not `gs://`) so they're never confused with real
GCS data — the 2 backends are never mixed within a single run since only one
of the two is selected in worker/services/storage.py.

The "signed URL" here has NO real signature (no private key needed) — it's
just a URL pointing at a PUT endpoint on the api service itself (see
api/routers/local_storage.py). This security tradeoff is accepted because
this is dev-only — USE_LOCAL_STORAGE=true must never be set on a real
deployment (see the warning in api/core/config.py).
"""

import asyncio
import shutil
from pathlib import Path
from urllib.parse import quote

_URI_SCHEME = "local://"


def _safe_path(storage_root: Path, blob_name: str) -> Path:
    """Resolves blob_name into an absolute path inside storage_root, raising
    if blob_name (e.g. containing "../..") tries to escape it — blob_name can
    contain client-controlled data (a filename the user uploaded, see
    api/routers/projects.py:create_upload_url), unlike GCS (a flat key-value
    store where "/" is just a naming convention) — on a real filesystem,
    traversal MUST be blocked."""
    root = storage_root.resolve()
    candidate = (root / blob_name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Invalid blob_name (path traversal?): {blob_name!r}")
    return candidate


class LocalStorageClient:
    def __init__(self, storage_dir: str, public_base_url: str):
        self._root = Path(storage_dir)
        self._public_base_url = public_base_url.rstrip("/")

    def _uri_to_blob_name(self, uri: str) -> str:
        if not uri.startswith(_URI_SCHEME):
            raise ValueError(f"Not a local storage URI: {uri!r}")
        return uri.removeprefix(_URI_SCHEME)

    def blob_uri(self, blob_name: str) -> str:
        """Just builds the URI string, no I/O — see GCSClient.blob_uri."""
        return f"{_URI_SCHEME}{blob_name}"

    async def download_to_path(self, uri: str, local_path: str) -> None:
        blob_name = self._uri_to_blob_name(uri)
        src = _safe_path(self._root, blob_name)

        def _copy():
            shutil.copyfile(src, local_path)

        await asyncio.to_thread(_copy)

    async def upload_from_path(self, local_path: str, blob_name: str) -> str:
        dest = _safe_path(self._root, blob_name)

        def _copy():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, dest)

        await asyncio.to_thread(_copy)
        return self.blob_uri(blob_name)

    async def upload_bytes(self, data: bytes, blob_name: str, content_type: str) -> str:
        # content_type isn't used here (the local filesystem doesn't store
        # this kind of metadata) — accepted as a parameter just to keep the
        # interface matching GCSClient.
        del content_type
        dest = _safe_path(self._root, blob_name)

        def _write():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        await asyncio.to_thread(_write)
        return self.blob_uri(blob_name)

    async def generate_signed_url(
        self, blob_name: str, content_type: str, expiration_minutes: int = 15
    ) -> str:
        # No real signature, no real expiration — see the warning in the
        # module docstring. The expiration_minutes/content_type parameters
        # are kept to match the StorageClient interface, unused on the local
        # branch.
        del content_type, expiration_minutes
        return f"{self._public_base_url}/local-storage/{quote(blob_name)}"

    async def generate_signed_read_url(
        self, gcs_uri: str, expiration_minutes: int = 60, download_filename: str | None = None
    ) -> str:
        # GET /local-storage/{blob_name} needs no auth/signature in local dev
        # (see api/routers/local_storage.py) — the parameter is kept only to
        # match the StorageClient interface; expiration_minutes is unused.
        del expiration_minutes
        blob_name = self._uri_to_blob_name(gcs_uri)
        url = f"{self._public_base_url}/local-storage/{quote(blob_name)}"
        if download_filename:
            # Local dev equivalent of the response_disposition on a real GCS
            # signed URL (see GCSClient.generate_signed_read_url) — the
            # router reads this query param to set the Content-Disposition
            # header itself (see api/routers/local_storage.py::get_object).
            url += f"?download_filename={quote(download_filename)}"
        return url

    async def delete(self, uri: str) -> None:
        """Idempotent — see GCSClient.delete for why a missing file is not an
        error here."""
        blob_name = self._uri_to_blob_name(uri)
        path = _safe_path(self._root, blob_name)

        def _remove():
            path.unlink(missing_ok=True)

        await asyncio.to_thread(_remove)
