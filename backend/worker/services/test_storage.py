"""Tests for build_storage_client() — picks the right implementation based on
settings.use_local_storage (see review: this closes a gap where the factory
itself had no direct test, even though both implementations already have
their own tests)."""

from worker.services.gcs_client import GCSClient
from worker.services.local_storage_client import LocalStorageClient
from worker.services.storage import build_storage_client


def test_returns_gcs_client_by_default(monkeypatch):
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_storage", False)
    client = build_storage_client()
    assert isinstance(client, GCSClient)


def test_returns_local_storage_client_when_enabled(monkeypatch):
    from api.core.config import settings

    monkeypatch.setattr(settings, "use_local_storage", True)
    client = build_storage_client()
    assert isinstance(client, LocalStorageClient)
