"""Replaces real Pub/Sub (see api/core/pubsub_client.py) with a direct HTTP call
to the worker's /pubsub/push endpoint — local dev has no real GCP credentials
to publish messages to Cloud Pub/Sub.

Still respects the architectural boundary "the API never calls worker/ code
directly" (see pubsub_client.py docstring) — this is a real HTTP request over
the network (container api -> container worker), not importing worker code into
the api process.

Still respects AGENTS.md principle #4 (the API must not block waiting for an
agent to finish): uses FastAPI BackgroundTasks — the request to the worker only
starts AFTER the POST /jobs response has been sent to the client, not on the
synchronous path that returns job_id.

MUST NOT be used when deploying for real — settings.use_local_pubsub defaults to
False, infra/deploy.sh does not set this variable (see the warning in api/core/config.py).
"""

import base64
import json
import logging

import httpx
from fastapi import BackgroundTasks

from api.core.config import settings

logger = logging.getLogger(__name__)

# Does not use a short timeout (unlike a real Pub/Sub push, which has a max ack
# deadline of ~600s) because the worker processes the whole pipeline synchronously
# in 1 request (see Dockerfile.worker: --timeout-keep-alive 3600, the same pattern
# already used in production). Still, a very long value is set (instead of
# None/infinite) as a safety net — if the worker hangs completely (deadlock/infinite
# loop), this httpx connection doesn't leak indefinitely across multiple dev-loop
# iterations within the same `docker compose up` session.
_DELIVERY_TIMEOUT_SECONDS = 7200.0


async def _deliver_message(payload: dict) -> None:
    """Builds the exact envelope that worker/main.py:handle_pubsub_push expects
    to receive from a real Pub/Sub push subscription, then POSTs it directly to
    the worker — shared by every message type (job/voice_sample...), see
    api/core/pubsub_client.py for why they're distinguished by the `type` field."""
    envelope = {
        "message": {"data": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")}
    }
    url = f"{settings.worker_internal_base_url}/pubsub/push"
    try:
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=envelope)
            response.raise_for_status()
    except Exception:  # noqa: BLE001 - background task, no caller to propagate the error to
        logger.exception("local pubsub: call to worker failed for message %r", payload)


def schedule_local_job_created(background_tasks: BackgroundTasks, job_id: str) -> None:
    background_tasks.add_task(_deliver_message, {"type": "job", "job_id": job_id})


def schedule_local_job_export_requested(background_tasks: BackgroundTasks, job_id: str) -> None:
    background_tasks.add_task(_deliver_message, {"type": "job_export", "job_id": job_id})


def schedule_local_job_attach_video_requested(
    background_tasks: BackgroundTasks, job_id: str
) -> None:
    background_tasks.add_task(_deliver_message, {"type": "job_attach_video", "job_id": job_id})


def schedule_local_voice_sample_uploaded(
    background_tasks: BackgroundTasks, character_id: str
) -> None:
    background_tasks.add_task(
        _deliver_message, {"type": "voice_sample", "character_id": character_id}
    )


def schedule_local_line_redub(background_tasks: BackgroundTasks, line_id: str) -> None:
    background_tasks.add_task(_deliver_message, {"type": "line_redub", "line_id": line_id})

