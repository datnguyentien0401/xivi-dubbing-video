"""Cloud Storage wrapper — reads/writes original and processed video and audio.

google-cloud-storage is a sync library (blocking network I/O) — every method
here runs via asyncio.to_thread so it doesn't block the event loop (see the
backend AGENTS.md: every I/O function must be async def).
"""

import asyncio

from google.cloud import storage


class GCSClient:
    def __init__(self, bucket_name: str):
        self._bucket_name = bucket_name
        # Lazy — storage.Client() calls google.auth.default() immediately on
        # construction, which raises if ADC isn't set up. Defer it to the first
        # real call so importing the module (e.g. api/main.py at module level)
        # doesn't require credentials to already be available (e.g. running
        # tests/lint locally without GCP auth).
        self._client: storage.Client | None = None

    def _get_bucket(self) -> storage.Bucket:
        if self._client is None:
            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def blob_uri(self, blob_name: str) -> str:
        """Just builds the URI string, no I/O — used when you need to know a
        blob's URI ahead of time (e.g. a DB record before the client actually
        uploads, see api/routers/projects.py:create_upload_url)."""
        return f"gs://{self._bucket_name}/{blob_name}"

    async def download_to_path(self, gcs_uri: str, local_path: str) -> None:
        bucket = self._get_bucket()
        blob_name = gcs_uri.removeprefix(f"gs://{bucket.name}/")
        await asyncio.to_thread(bucket.blob(blob_name).download_to_filename, local_path)

    async def upload_from_path(self, local_path: str, blob_name: str) -> str:
        bucket = self._get_bucket()
        blob = bucket.blob(blob_name)
        await asyncio.to_thread(blob.upload_from_filename, local_path)
        return self.blob_uri(blob_name)

    async def upload_bytes(self, data: bytes, blob_name: str, content_type: str) -> str:
        bucket = self._get_bucket()
        blob = bucket.blob(blob_name)
        await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type)
        return self.blob_uri(blob_name)

    def _sign_blob_url(
        self,
        blob_name: str,
        method: str,
        content_type: str | None,
        expiration_minutes: int,
        response_disposition: str | None = None,
    ) -> str:
        """On Cloud Run, ADC is just an access token from the metadata server
        (no local private key like a service account key file), so
        `blob.generate_signed_url()` by default raises AttributeError ("you
        need a private key to sign credentials") — this was personally
        reproduced with a real request. Must force use of the IAM signBlob
        API instead of signing locally, by passing the current
        service_account_email + access_token directly; the SA running Cloud
        Run needs the roles/iam.serviceAccountTokenCreator permission on
        itself for this signBlob call to succeed (already granted in
        infra/deploy.sh)."""
        from datetime import timedelta

        import google.auth
        from google.auth.transport import requests as google_requests

        credentials, _ = google.auth.default()
        credentials.refresh(google_requests.Request())
        blob = self._get_bucket().blob(blob_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method=method,
            content_type=content_type,
            response_disposition=response_disposition,
            service_account_email=credentials.service_account_email,
            access_token=credentials.token,
        )

    async def generate_signed_url(
        self, blob_name: str, content_type: str, expiration_minutes: int = 15
    ) -> str:
        """Signed URL for upload (PUT) — see create_upload_url in api/routers/projects.py."""
        return await asyncio.to_thread(
            self._sign_blob_url, blob_name, "PUT", content_type, expiration_minutes
        )

    async def generate_signed_read_url(
        self, gcs_uri: str, expiration_minutes: int = 60, download_filename: str | None = None
    ) -> str:
        """Signed URL for reading (GET) — the bucket doesn't allow public
        read (uniform bucket-level access, see infra/deploy.sh) so
        audio/video CANNOT be played directly via
        https://storage.googleapis.com/... the way AudioPlayer.tsx used to
        assume (a 403 was personally reproduced while testing on the cloud).
        Used for playing back dubbed audio (ReviewDashboard) and character
        voice samples (VoiceManager).

        `download_filename`: when passed, the signed URL includes
        response_disposition so the browser DOWNLOADS the file instead of
        opening/playing it directly (see get_job_share_url in
        api/routers/jobs.py) — GCS automatically adds the corresponding
        Content-Disposition header when serving this URL, so the backend
        doesn't need to set the header itself at that point (the GET request
        to the signed URL goes straight to GCS, not through the app)."""
        bucket = self._get_bucket()
        blob_name = gcs_uri.removeprefix(f"gs://{bucket.name}/")
        response_disposition = (
            f'attachment; filename="{download_filename}"' if download_filename else None
        )
        return await asyncio.to_thread(
            self._sign_blob_url, blob_name, "GET", None, expiration_minutes, response_disposition
        )
