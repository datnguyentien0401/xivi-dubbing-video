"""Tests for api/routers/local_storage.py — the PUT/GET endpoint that fakes
GCS signed URLs for local dev, and the path-traversal guard (this is a
second, independent entry point from worker/services/local_storage_client.py,
since blob_name comes straight from the URL path sent by the client — see
test_local_storage_client.py for the guard at the client layer)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import local_storage


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage.settings, "local_storage_dir", str(tmp_path))
    app = FastAPI()
    app.include_router(local_storage.router)
    return TestClient(app)


def test_put_then_get_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    put_resp = client.put("/local-storage/jobs/j1/dubbed/l1.wav", content=b"hello audio")
    assert put_resp.status_code == 200

    get_resp = client.get("/local-storage/jobs/j1/dubbed/l1.wav")
    assert get_resp.status_code == 200
    assert get_resp.content == b"hello audio"


def test_put_creates_nested_directories(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/local-storage/a/b/c/d.bin", content=b"nested")
    assert resp.status_code == 200
    assert (tmp_path / "a" / "b" / "c" / "d.bin").read_bytes() == b"nested"


def test_get_sets_no_store_cache_control(tmp_path, monkeypatch):
    """Regression test for a real bug: re-dubbing a line overwrites the file
    at the SAME blob_name (e.g. jobs/{job_id}/dubbed/{line_id}.wav) — without
    Cache-Control, the browser might keep playing the OLD cached audio for
    that same URL even though the backend has generated the correct new audio
    on disk (unlike a real GCS signed URL, which always has its own
    expiration query param each time and so naturally busts the cache)."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/jobs/j1/dubbed/l1.wav", content=b"v1")

    get_resp = client.get("/local-storage/jobs/j1/dubbed/l1.wav")

    assert get_resp.headers["cache-control"] == "no-store"


def test_get_missing_object_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/local-storage/does/not/exist.mp4")
    assert resp.status_code == 404


def test_put_rejects_path_traversal(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/local-storage/../../etc/evil.txt", content=b"pwned")
    # Starlette normalizes "../" in the path before route matching with the
    # httpx test client (no real network in between), so this request
    # actually resolves outside the /local-storage/ prefix -> 404 route not
    # found, rather than reaching the handler. The _safe_path guard is a
    # second line of defense for traversal cases not blocked at the routing
    # layer (e.g. blob_name built by other code concatenating strings and
    # calling _safe_path directly) — tested separately below.
    assert resp.status_code == 404


def test_safe_path_rejects_traversal_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage.settings, "local_storage_dir", str(tmp_path))
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        local_storage._safe_path("../../etc/passwd")
    assert exc_info.value.status_code == 400


def test_get_advertises_accept_ranges(tmp_path, monkeypatch):
    """Video Editor Timeline.tsx needs to seek video — the browser only sends
    a Range request on its own if the server has advertised support via this
    header."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4")

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"0123456789"


def test_get_with_range_header_returns_206_partial_content(tmp_path, monkeypatch):
    """Regression test: previously FileResponse (Starlette 0.38.6) completely
    ignored the Range header, always returning 200 + the whole file — making
    <video> unseekable (video.seekable was always empty even though readyState
    was already HAVE_ENOUGH_DATA, manually reproduced via Timeline.tsx:
    clicking a clip didn't move the playhead). Must return exactly 206 + the
    correct requested byte range."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=2-4"})

    assert resp.status_code == 206
    assert resp.content == b"234"
    assert resp.headers["content-range"] == "bytes 2-4/10"
    assert resp.headers["content-length"] == "3"
    assert resp.headers["accept-ranges"] == "bytes"


def test_get_with_open_ended_range_returns_rest_of_file(tmp_path, monkeypatch):
    """ "bytes=5-" (no end) — a form browsers commonly use when first starting
    to seek, meaning "from byte 5 to the end of the file", not a missing-parameter error."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=5-"})

    assert resp.status_code == 206
    assert resp.content == b"56789"
    assert resp.headers["content-range"] == "bytes 5-9/10"


def test_get_with_range_beyond_file_size_returns_416(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=100-200"})

    assert resp.status_code == 416


def test_get_with_malformed_range_returns_416(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "not-a-range"})

    assert resp.status_code == 416


def test_get_with_suffix_range_returns_last_n_bytes(tmp_path, monkeypatch):
    """Regression test (discovered during review): "bytes=-N" (suffix-range,
    RFC 7233 §2.1) means "the last N bytes of the file" — some media players
    use this form to read the trailing moov atom of a non-faststart MP4.
    Before the fix, the regex still matched but was MISINTERPRETED as
    start=0 (i.e. "the first 6 bytes"), returning a 206 with WRONG data
    instead of reporting an error — worse than not supporting it at all."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=-5"})

    assert resp.status_code == 206
    assert resp.content == b"56789"
    assert resp.headers["content-range"] == "bytes 5-9/10"


def test_get_with_suffix_range_larger_than_file_returns_whole_file(tmp_path, monkeypatch):
    """ "bytes=-100" on a file that's only 10 bytes — a suffix-range larger
    than the whole file means "the entire file" (RFC 7233 §2.1: clamp to 0),
    not a 416 error."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=-100"})

    assert resp.status_code == 206
    assert resp.content == b"0123456789"
    assert resp.headers["content-range"] == "bytes 0-9/10"


def test_get_with_empty_range_value_returns_416(tmp_path, monkeypatch):
    """ "bytes=-" (missing both start and end) — not a valid suffix-range,
    must report a clear error instead of silently interpreting it as "the
    entire file"."""
    client = _client(tmp_path, monkeypatch)
    client.put("/local-storage/video.mp4", content=b"0123456789")

    resp = client.get("/local-storage/video.mp4", headers={"Range": "bytes=-"})

    assert resp.status_code == 416
