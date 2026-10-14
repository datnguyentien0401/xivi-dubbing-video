"""Tests for local_pubsub_client.py — builds the correct simulated Pub/Sub push
envelope and calls the correct internal worker URL (no real HTTP call, mocks
httpx.AsyncClient)."""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from api.core.local_pubsub_client import (
    _deliver_message,
    schedule_local_job_created,
    schedule_local_job_export_requested,
    schedule_local_voice_sample_uploaded,
)


@pytest.fixture(autouse=True)
def _worker_url(monkeypatch):
    from api.core.config import settings

    monkeypatch.setattr(settings, "worker_internal_base_url", "http://worker:8080")


async def test_deliver_message_posts_correct_envelope_to_pubsub_push_url():
    mock_response = AsyncMock()
    mock_response.raise_for_status = lambda: None

    with patch("api.core.local_pubsub_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await _deliver_message({"type": "job", "job_id": "job-123"})

        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.await_args
        assert call_args.args[0] == "http://worker:8080/pubsub/push"

        envelope = call_args.kwargs["json"]
        decoded = json.loads(base64.b64decode(envelope["message"]["data"]))
        assert decoded == {"type": "job", "job_id": "job-123"}


async def test_deliver_message_swallows_errors_no_caller_to_propagate_to():
    with patch("api.core.local_pubsub_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("worker unreachable"))
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Does not raise — this is a background task, nowhere awaits the
        # result to catch the exception (see BackgroundTasks in FastAPI).
        await _deliver_message({"type": "job", "job_id": "job-456"})


def test_schedule_local_job_created_registers_background_task():
    background_tasks = BackgroundTasks()
    schedule_local_job_created(background_tasks, "job-789")
    assert len(background_tasks.tasks) == 1


def test_schedule_local_voice_sample_uploaded_registers_background_task():
    background_tasks = BackgroundTasks()
    schedule_local_voice_sample_uploaded(background_tasks, "char-1")
    assert len(background_tasks.tasks) == 1


def test_schedule_local_job_export_requested_registers_background_task():
    background_tasks = BackgroundTasks()
    schedule_local_job_export_requested(background_tasks, "job-999")
    assert len(background_tasks.tasks) == 1
