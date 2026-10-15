#!/usr/bin/env bash
# Manual deploy to Cloud Run — used for the hackathon stage (no Terraform needed yet).
#
# Prerequisites before running (CANNOT be automated via this script — see the README at
# the end of the file for why):
#   1. The secret `gemini-api-key` already exists in Secret Manager (a real Gemini
#      API key value). Create it with:
#        printf '%s' "<real key>" | gcloud secrets create gemini-api-key \
#          --project "$PROJECT_ID" --data-file=-
#   2. The secret `redis-url` already exists (external Redis, e.g. Upstash free tier —
#      see the README at the end of this file). Create it with:
#        printf '%s' "<real redis:// URL>" | gcloud secrets create redis-url \
#          --project "$PROJECT_ID" --data-file=-
#   3. The secret `database-url` — this script creates it itself if missing (using the
#      Cloud SQL instance created in the step below), no need to create it by hand.
#
# Run: bash infra/deploy.sh <GCP_PROJECT_ID> <REGION>
set -euo pipefail

PROJECT_ID="${1:?Must pass a GCP project ID}"
REGION="${2:-asia-southeast1}"
SA_EMAIL="toonlingo-run-sa@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET_NAME="${GCS_BUCKET_NAME:-${PROJECT_ID}-media}"
SQL_INSTANCE="toonlingo-pg"
INSTANCE_CONN="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

echo "== Enabling required APIs (idempotent) =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com pubsub.googleapis.com storage.googleapis.com \
  firestore.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com identitytoolkit.googleapis.com \
  --project "$PROJECT_ID" 2>&1 | tail -1

echo "== GCS bucket for storing media (idempotent) =="
gcloud storage buckets create "gs://${BUCKET_NAME}" \
  --project "$PROJECT_ID" --location "$REGION" \
  --uniform-bucket-level-access 2>&1 || true

# CORS to let the frontend (browser) upload via signed URL (PUT) and play back
# video/audio via signed URL (GET) — without this step, browser-side fetch
# is completely blocked by CORS. Important: "Range" MUST be in responseHeader
# — without it, any preflight request carrying Range (which is exactly how <video>/<audio>
# browser tags seek/stream) is rejected entirely by GCS (it returns NO
# access-control-* headers at all, including access-control-allow-origin — not
# just a missing Range specifically), which means video/audio will NOT play even though the file and
# signed URL are completely valid (reproduced by hand: ReviewDashboard.tsx showed the
# video "crashing"/failing to play; testing the OPTIONS preflight directly with
# Access-Control-Request-Headers: range confirmed GCS returns zero CORS headers).
cat > /tmp/toonlingo-cors.json <<'CORS_EOF'
[
  {
    "origin": ["*"],
    "method": ["GET", "PUT", "HEAD"],
    "responseHeader": ["Content-Type", "x-goog-resumable", "Range", "Accept-Ranges", "Content-Range", "Content-Length"],
    "maxAgeSeconds": 3600
  }
]
CORS_EOF
gcloud storage buckets update "gs://${BUCKET_NAME}" \
  --cors-file=/tmp/toonlingo-cors.json --project "$PROJECT_ID" 2>&1 | tail -3
rm -f /tmp/toonlingo-cors.json

echo "== Cloud SQL Postgres — smallest tier db-f1-micro (idempotent, may take a few minutes the first time) =="
if ! gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1; then
  DB_ROOT_PASSWORD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)
  gcloud sql instances create "$SQL_INSTANCE" \
    --project "$PROJECT_ID" --database-version POSTGRES_16 \
    --edition ENTERPRISE --tier db-f1-micro --region "$REGION" \
    --storage-size 10 --storage-type HDD --no-backup \
    --root-password "$DB_ROOT_PASSWORD"
fi

echo "== App DB + dedicated user (never use root for the application) =="
gcloud sql databases create toonlingo --instance "$SQL_INSTANCE" --project "$PROJECT_ID" 2>&1 || true
if ! gcloud sql users list --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --format="value(name)" | grep -qx toonlingo; then
  APP_DB_PASSWORD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)
  gcloud sql users create toonlingo --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --password "$APP_DB_PASSWORD"
  DATABASE_URL="postgresql+asyncpg://toonlingo:${APP_DB_PASSWORD}@/toonlingo?host=/cloudsql/${INSTANCE_CONN}"
  printf '%s' "$DATABASE_URL" | gcloud secrets create database-url --project "$PROJECT_ID" --data-file=- 2>&1 || \
  printf '%s' "$DATABASE_URL" | gcloud secrets versions add database-url --project "$PROJECT_ID" --data-file=-
fi

if ! gcloud secrets describe database-url --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "ERROR: secret database-url doesn't exist yet, and user 'toonlingo' already exists on the instance —" >&2
  echo "the password can't be inferred. Create the database-url secret by hand, then re-run." >&2
  exit 1
fi

echo "== Dedicated service account for the Cloud Run runtime (idempotent) =="
gcloud iam service-accounts create toonlingo-run-sa \
  --project "$PROJECT_ID" --display-name "ToonLingo Cloud Run runtime SA" 2>&1 || true

echo "== Granting permissions to that service account (idempotent) =="
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member "serviceAccount:${SA_EMAIL}" --role "roles/storage.objectAdmin" \
  --project "$PROJECT_ID" 2>&1 | tail -1
# Needs to grant itself sign (SignBlob) permission — Cloud Run has no local
# private key to sign signed URLs (v4) the way running on a personal machine with a
# service account key file would, so it must ask the IAM API to sign on its behalf (see worker/services/gcs_client.py:generate_signed_url).
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member "serviceAccount:${SA_EMAIL}" --role "roles/iam.serviceAccountTokenCreator" \
  --project "$PROJECT_ID" 2>&1 | tail -1
for role in roles/cloudsql.client roles/datastore.user roles/pubsub.publisher roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" --role "$role" --condition=None 2>&1 | tail -1
done

echo "== Creating Pub/Sub topic =="
gcloud pubsub topics create agent-events --project "$PROJECT_ID" 2>&1 || true

# Non-sensitive env vars are set directly via --set-env-vars; secret values
# (GEMINI_API_KEY, DATABASE_URL, REDIS_URL) go via --set-secrets, and NEVER
# through --set-env-vars (which would expose them as plaintext in `gcloud run services
# describe` / Cloud Console — see AGENTS.md section 4 "Never commit secrets").
COMMON_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GCS_BUCKET_NAME=${BUCKET_NAME},FIREBASE_PROJECT_ID=${PROJECT_ID}"
COMMON_SECRETS="GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest,REDIS_URL=redis-url:latest"

echo "== Artifact Registry Docker repo (idempotent) =="
gcloud artifacts repositories create cloud-run-source-deploy \
  --project "$PROJECT_ID" --location "$REGION" --repository-format docker 2>&1 || true
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet 2>&1 | tail -1

AR_PREFIX="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy"

# `gcloud run deploy --source` only auto-selects a Dockerfile when it's named exactly "Dockerfile"
# at the directory root — backend/ has 2 files (Dockerfile.api, Dockerfile.worker), so
# it always falls back to Buildpacks (a wrong build, which actually failed at this step before)
# instead of using the intended Dockerfile. Build + push manually with the right Dockerfile
# then deploy via --image to eliminate that ambiguity entirely.
# --platform linux/amd64 is required when building on Apple Silicon (arm64) —
# Cloud Run needs an amd64 image, and a default build on a dev machine would produce arm64 and deploy
# would fail with "no matching manifest" if the platform isn't forced.
echo "== Build + push API image =="
docker build --platform linux/amd64 -f backend/Dockerfile.api -t "${AR_PREFIX}/toonlingo-api:latest" backend
docker push "${AR_PREFIX}/toonlingo-api:latest"

echo "== Build + push Worker image =="
docker build --platform linux/amd64 -f backend/Dockerfile.worker -t "${AR_PREFIX}/toonlingo-worker:latest" backend
docker push "${AR_PREFIX}/toonlingo-worker:latest"

# Run BEFORE deploying the API/Worker services below — the new code in this
# deploy may assume columns/tables that don't exist yet, see the real
# incident this fixed: Cloud SQL had never run a migration past the very
# first deploy (nothing in this script did it), so `jobs.project_id` (added
# by a later migration, specs/003-script-first-ingestion) didn't exist,
# every GET /projects/{id}/jobs call raised UndefinedColumnError, and the
# Project Detail page silently showed "No translations yet" instead of the
# real jobs. Uses the SAME image as the `api` service (just a different
# entrypoint) — exactly mirrors docker-compose.yml's local `migrate` service.
# `alembic upgrade head` is idempotent (a no-op if already current), so safe
# to run on every deploy, not just when a migration was actually added.
echo "== Run DB migrations (alembic upgrade head — idempotent) =="
gcloud run jobs deploy toonlingo-migrate \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "${AR_PREFIX}/toonlingo-api:latest" \
  --command alembic --args upgrade,head \
  --service-account "$SA_EMAIL" \
  --set-cloudsql-instances "$INSTANCE_CONN" \
  --set-env-vars "$COMMON_ENV" --set-secrets "$COMMON_SECRETS" \
  --max-retries 0 \
  --execute-now --wait

echo "== Deploy API service =="
gcloud run deploy toonlingo-api \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "${AR_PREFIX}/toonlingo-api:latest" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$INSTANCE_CONN" \
  --set-env-vars "$COMMON_ENV" --set-secrets "$COMMON_SECRETS" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 10

echo "== Deploy Worker service (1st pass — temporary audience, fixed up below once the real URL exists) =="
# --memory=2Gi --cpu=2: Cloud Run's default (512Mi/1 CPU) isn't enough for the
# mux step (worker/services/mux_service.py) since it burns in subtitles + re-encodes the
# whole video with libx264/libass instead of just stream-copying — a real
# OOM kill on Cloud Run was reproduced by hand (log "Memory limit of 512 MiB exceeded with
# ~630 MiB used"), which left the job "hanging" partway through the mux step for no obvious reason.
gcloud run deploy toonlingo-worker \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "${AR_PREFIX}/toonlingo-worker:latest" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$INSTANCE_CONN" \
  --set-env-vars "$COMMON_ENV" --set-secrets "$COMMON_SECRETS" \
  --no-allow-unauthenticated \
  --memory=2Gi --cpu=2 \
  --min-instances 0 --max-instances 5 \
  --timeout 3600 --concurrency 1

WORKER_URL=$(gcloud run services describe toonlingo-worker --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
INVOKER_SA="toonlingo-pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

echo "== Fixing up PUBSUB_PUSH_AUDIENCE to the real URL just allocated =="
gcloud run services update toonlingo-worker \
  --project "$PROJECT_ID" --region "$REGION" \
  --update-env-vars "PUBSUB_PUSH_AUDIENCE=${WORKER_URL}/pubsub/push"

echo "== Creating the service account for Pub/Sub push auth (if not already present) =="
gcloud iam service-accounts create toonlingo-pubsub-invoker \
  --project "$PROJECT_ID" \
  --display-name "ToonLingo Pub/Sub push invoker" || true

echo "== Granting that service account permission to call the worker (run.invoker) =="
gcloud run services add-iam-policy-binding toonlingo-worker \
  --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:${INVOKER_SA}" --role "roles/run.invoker"

echo "== Creating a push subscription pointing at the worker: $WORKER_URL =="
# ack-deadline=600 (the maximum value Pub/Sub push allows) — gcloud's default is
# only 10s, while the /pubsub/push handler BLOCKS the response until the whole pipeline
# finishes (several minutes, especially now that the mux step has to re-encode the whole
# video, see worker/services/mux_service.py). This huge mismatch causes Pub/Sub to
# continuously redeliver the message for the ENTIRE duration of processing — reproduced by hand: one job
# got redelivered dozens of times within a few dozen seconds despite no error at all, leading to
# duplicate requests running concurrently (the lock in worker/main.py prevents data corruption
# but doesn't prevent the wasted work). A very long video (>10 minutes of processing) could still
# exceed this 600s ceiling — see the long-term architectural TODO: decoupling the
# ack from waiting for processing to finish (e.g. Cloud Tasks) instead of blocking the response.
gcloud pubsub subscriptions create agent-events-worker-sub \
  --project "$PROJECT_ID" --topic agent-events \
  --push-endpoint "${WORKER_URL}/pubsub/push" \
  --push-auth-service-account "$INVOKER_SA" \
  --ack-deadline=600 || \
gcloud pubsub subscriptions update agent-events-worker-sub \
  --project "$PROJECT_ID" --ack-deadline=600

API_URL=$(gcloud run services describe toonlingo-api --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
echo ""
echo "Done."
echo "  API URL:    $API_URL"
echo "  Worker URL: $WORKER_URL (internal, not allow-unauthenticated)"
echo ""
echo "Remaining steps (manual, see README below):"
echo "  - Build the frontend with the real VITE_API_BASE_URL=$API_URL and VITE_FIREBASE_*,"
echo "    then deploy it to Firebase Hosting."

# --- Short README: why these 3 steps CANNOT be automated ---
# 1. gemini-api-key / redis-url: these are third-party secrets (Google AI
#    Studio, Upstash) that an AI agent is NOT allowed to enter/read on the user's
#    behalf (see the harness's "Prohibited actions" rule) — the user must create the
#    Secret Manager secret containing that value themselves (sample command at the top of the file).
# 2. Redis: this project chose to use an external free tier (e.g. Upstash) instead of
#    Memorystore (decision finalized via AskUserQuestion) — Upstash needs its own
#    account, and there's no gcloud API/CLI that can create one on the user's behalf.
# 3. Deploying the frontend to Firebase Hosting: needs `firebase login` (interactive
#    OAuth) + `firebase deploy` — outside the scope of this gcloud script,
#    done as a separate step once the real API_URL exists.
