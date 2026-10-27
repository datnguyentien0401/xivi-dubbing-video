"""Project configuration, read from environment variables (see .env.example at root)."""

import json
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_cloud_project: str
    gemini_api_key: str = ""
    database_url: str
    redis_url: str
    pubsub_topic_agent_events: str = "agent-events"
    pubsub_push_audience: str = ""
    gcs_bucket_name: str
    firebase_project_id: str
    cors_origins: list[str] = [
        "https://xivi-studio.web.app",
        "https://xivi-studio.firebaseapp.com",
        "https://toonlingo-app.web.app",
        "https://toonlingo-app.firebaseapp.com",
        "http://localhost:5173",
        "http://localhost:5183",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5183",
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            if v.startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # ONLY enable ("true") in docker-compose.yml for local dev — skips verifying
    # a real Firebase ID token, using 1 fixed user instead (see api/core/firebase_auth.py).
    # MUST NOT be set when deploying for real (infra/deploy.sh does not set it,
    # Cloud Run defaults to False since Settings has a default here).
    auth_dev_bypass: bool = False
    # ONLY enable in docker-compose.yml — replaces GCSClient with LocalStorageClient
    # (stores files on a local volume via the /local-storage endpoint on the api
    # service itself, see worker/services/storage.py) because local dev has no
    # real GCP service account to sign signed URLs / call Cloud Storage. MUST NOT
    # be set when deploying for real (infra/deploy.sh does not set it).
    use_local_storage: bool = False
    local_storage_dir: str = "/data/local-storage"
    # URL the browser/host uses to PUT/GET files via local-storage — MUST be the
    # port published to the host (e.g. http://localhost:8010), not the internal
    # compose network hostname ("http://api:8080") because the browser runs outside
    # that network (same reason as VITE_API_BASE_URL in docker-compose.yml).
    local_storage_public_base_url: str = "http://localhost:8000"
    # ONLY enable in docker-compose.yml — replaces publishing to real Pub/Sub (needs
    # GCP credentials, see api/core/pubsub_client.py) with a direct HTTP call to the
    # /pubsub/push endpoint on the worker service itself (see
    # api/core/local_pubsub_client.py), via BackgroundTasks so it still follows
    # AGENTS.md principle #4 (the API must not block waiting for an agent to finish).
    # The worker also reads this flag to skip verifying a real OIDC token (there is
    # no real Pub/Sub call coming in, so there's no real token to verify). MUST NOT
    # be set when deploying for real (infra/deploy.sh does not set it).
    use_local_pubsub: bool = False
    # Internal compose network hostname (NOT the host-published port like
    # local_storage_public_base_url above) — this request goes container-to-
    # container (api -> worker), not from the browser.
    worker_internal_base_url: str = "http://localhost:8001"
    # ONLY enable in docker-compose.yml — skips writing to Firestore (needs real
    # GCP credentials, see worker/main.py:_get_firestore). Does not attempt to
    # connect to Firestore on every stage transition (costly, spams logs); instead
    # relies solely on the try/except in _update_realtime_status. Consequence: the
    # frontend's PipelineStatus.tsx (Firestore realtime listener) will NOT receive
    # updates in local dev — see GET /jobs/{id} for manual polling instead. MUST NOT
    # be set when deploying for real.
    use_local_firestore: bool = False


settings = Settings()  # type: ignore[call-arg]  -- required fields come from .env
