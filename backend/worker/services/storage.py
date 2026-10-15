"""Common interface for GCSClient/LocalStorageClient + a factory that picks
the implementation based on settings.use_local_storage — every call site
(api/worker) MUST get the client via `build_storage_client()`, never
`GCSClient(...)` directly, so switching the storage backend doesn't require
changes in many places (see AGENTS.md principle #6 — don't hardcode; here
that means "don't hardcode the storage backend into the call site").
"""

from typing import Protocol

from api.core.config import settings
from worker.services.gcs_client import GCSClient
from worker.services.local_storage_client import LocalStorageClient


class StorageClient(Protocol):
    """The minimal interface that both GCSClient and LocalStorageClient
    satisfy. Uses a Protocol (structural typing) instead of a common base
    class — both classes already exist independently, no need to force
    inheritance just for type-checking."""

    def blob_uri(self, blob_name: str) -> str: ...

    async def download_to_path(self, uri: str, local_path: str) -> None: ...

    async def upload_from_path(self, local_path: str, blob_name: str) -> str: ...

    async def upload_bytes(self, data: bytes, blob_name: str, content_type: str) -> str: ...

    async def generate_signed_url(
        self, blob_name: str, content_type: str, expiration_minutes: int = 15
    ) -> str: ...

    async def generate_signed_read_url(
        self, gcs_uri: str, expiration_minutes: int = 60, download_filename: str | None = None
    ) -> str: ...

    async def delete(self, uri: str) -> None: ...


def build_storage_client() -> StorageClient:
    if settings.use_local_storage:
        return LocalStorageClient(
            storage_dir=settings.local_storage_dir,
            public_base_url=settings.local_storage_public_base_url,
        )
    return GCSClient(settings.gcs_bucket_name)
