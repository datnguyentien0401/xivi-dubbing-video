"""Tests for LocalStorageClient — in particular the path-traversal guard,
since blob_name can contain client-controlled data (a filename the user
uploaded, see api/routers/projects.py:create_upload_url) and here (unlike
GCS) blob_name actually maps to a filesystem path."""

import pytest

from worker.services.local_storage_client import LocalStorageClient


@pytest.fixture
def client(tmp_path):
    return LocalStorageClient(storage_dir=str(tmp_path), public_base_url="http://localhost:8010")


def test_blob_uri_uses_local_scheme(client):
    assert client.blob_uri("jobs/x/dubbed/y.wav") == "local://jobs/x/dubbed/y.wav"


async def test_upload_bytes_then_download_roundtrip(client, tmp_path):
    uri = await client.upload_bytes(b"hello audio", "jobs/j1/dubbed/l1.wav", "audio/wav")
    assert uri == "local://jobs/j1/dubbed/l1.wav"

    dest = tmp_path / "downloaded.wav"
    await client.download_to_path(uri, str(dest))
    assert dest.read_bytes() == b"hello audio"


async def test_upload_from_path_creates_nested_dirs(client, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake video bytes")

    uri = await client.upload_from_path(str(src), "jobs/j1/muxed/id.mp4")
    assert uri == "local://jobs/j1/muxed/id.mp4"

    dest = tmp_path / "roundtrip.mp4"
    await client.download_to_path(uri, str(dest))
    assert dest.read_bytes() == b"fake video bytes"


async def test_generate_signed_url_points_at_local_storage_endpoint(client):
    # urllib.parse.quote() defaults to safe="/" (unlike JS's
    # encodeURIComponent), so "/" is kept as-is instead of being encoded to
    # %2F — this is intentional because the FastAPI route "{blob_name:path}"
    # matches a multi-segment path directly.
    url = await client.generate_signed_url("projects/p1/source/video.mp4", content_type="video/mp4")
    assert url == "http://localhost:8010/local-storage/projects/p1/source/video.mp4"


async def test_generate_signed_read_url_accepts_local_uri(client):
    """Accepts the full local://... URI (like GCSClient's
    generate_signed_read_url accepts gs://...) rather than just blob_name —
    see api/routers/jobs.py:get_dialogue_line_audio_url."""
    url = await client.generate_signed_read_url("local://jobs/j1/dubbed/line1.wav")
    assert url == "http://localhost:8010/local-storage/jobs/j1/dubbed/line1.wav"


@pytest.mark.parametrize(
    "malicious_blob_name",
    [
        "../../etc/passwd",
        "../outside.txt",
        "jobs/../../escape.txt",
    ],
)
async def test_upload_bytes_rejects_path_traversal(client, malicious_blob_name):
    with pytest.raises(ValueError, match="path traversal"):
        await client.upload_bytes(b"x", malicious_blob_name, "text/plain")


async def test_download_to_path_rejects_path_traversal(client, tmp_path):
    dest = tmp_path / "out.txt"
    with pytest.raises(ValueError, match="path traversal"):
        await client.download_to_path("local://../../etc/passwd", str(dest))
