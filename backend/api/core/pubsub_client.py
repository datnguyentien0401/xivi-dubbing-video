"""Publishes a message to Pub/Sub for the worker to process asynchronously.

This is the only connection point between the api service and the worker
service — the API NEVER calls code inside worker/ directly, it only
publishes messages.
"""

import json

from google.cloud import pubsub_v1

from api.core.config import settings

_publisher: pubsub_v1.PublisherClient | None = None


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _publish(payload: dict) -> None:
    topic_path = _get_publisher().topic_path(
        settings.google_cloud_project, settings.pubsub_topic_agent_events
    )
    data = json.dumps(payload).encode("utf-8")
    _get_publisher().publish(topic_path, data)


def publish_job_created(job_id: str) -> None:
    """An explicit `type` lets worker/main.py::handle_pubsub_push distinguish
    this from other message kinds (e.g. publish_voice_sample_uploaded) on the
    SAME topic — reusing the existing topic/subscription instead of creating new
    Pub/Sub infrastructure for every kind of async work (see AGENTS.md #3:
    prefer managed GCP services, don't add a new message broker/infrastructure
    unless truly necessary). Old messages (published before the `type` field
    existed) don't have this field — the worker defaults a missing `type` to
    "job" for backward compatibility."""
    _publish({"type": "job", "job_id": job_id})


def publish_job_export_requested(job_id: str) -> None:
    """Triggers the worker to render an export from `jobs.timeline_edits` (Video
    Editor, specs/002-video-editor) — a separate `type`, NOT "job", because this
    is not a continuation of the sequential agent pipeline (worker/pipeline.py::
    _STAGE_ORDER has no "exporting" stage; it would be mistaken for a new job and
    rerun the ENTIRE pipeline from scratch if it fell into the "job" branch — see
    worker/main.py::_export_job)."""
    _publish({"type": "job_export", "job_id": job_id})


def publish_job_attach_video_requested(job_id: str) -> None:
    """Triggers the worker to run ONLY the "muxed" pipeline stage for a
    script-first job that just had a real video attached (US5,
    specs/003-script-first-ingestion) — a separate `type`, NOT "job", for
    the same reason as publish_job_export_requested above:
    worker/pipeline.py's _STAGE_ORDER resume logic would misinterpret this
    as a fresh/resumed run of the full pipeline (re-translating/re-dubbing
    everything) rather than "just mux what's already dubbed" (see
    worker/main.py::_attach_video)."""
    _publish({"type": "job_attach_video", "job_id": job_id})


def publish_voice_sample_uploaded(character_id: str) -> None:
    """Triggers the worker to listen to the voice sample a user just uploaded for
    a Character (see api/routers/characters.py::classify_voice_sample) and pick
    the closest-matching TTS voice — the same Gemini mechanism already used for
    automatic voice profiling from video (worker/agents/voice_profiling_agent.py),
    reused via worker/main.py::_classify_character_voice_sample."""
    _publish({"type": "voice_sample", "character_id": character_id})
