# System Architecture — XIVI Agent

## 1. Overall system diagram

```mermaid
graph TB
    subgraph Client["Frontend — React Studio Dashboard"]
        UI[Web App<br/>React + Vite]
    end

    subgraph GCP["Google Cloud Platform"]
        subgraph API["Cloud Run: API Service"]
            FastAPI[FastAPI Orchestrator]
        end

        subgraph Worker["Cloud Run: Worker Service"]
            Pipeline[Agent Pipeline Runner]
        end

        PubSub[(Cloud Pub/Sub<br/>agent-events topic)]
        GCS[(Cloud Storage<br/>video / audio files)]
        SQL[(Cloud SQL<br/>PostgreSQL)]
        FireStore[(Firestore<br/>job status realtime)]
        Redis[(Memorystore Redis<br/>cache + rate limit)]
        FireAuth[Firebase Auth]

        Gemini[Gemini API<br/>text / vision / native audio]
    end

    UI -->|HTTPS + Firebase ID token| FastAPI
    UI -->|realtime listener| FireStore
    FastAPI -->|read/write| SQL
    FastAPI -->|signed upload URL| GCS
    FastAPI -->|publish job| PubSub
    FastAPI --> FireAuth
    PubSub -->|push subscription| Pipeline
    Pipeline -->|read/write files| GCS
    Pipeline -->|read/write state| SQL
    Pipeline -->|realtime update| FireStore
    Pipeline -->|cache glossary/voice profile| Redis
    Pipeline -->|call model| Gemini
```

**Why 2 separate Cloud Run services (api vs worker)?**
- `api`: serves HTTP requests from the frontend, needs to respond fast (<1s),
  scales with user traffic
- `worker`: handles the long-running pipeline (translation, dubbing, audio
  muxing — can take several minutes per video), receives work via a Pub/Sub
  push subscription, scales with job volume, and never has a user directly
  waiting on an HTTP request to it

Keeping them separate gives us: (1) users don't time out while uploading
video, (2) the two very different kinds of load can scale independently,
(3) it follows GCP's recommended serverless pattern for long-running tasks.

## 2. Main data flow (request lifecycle)

```mermaid
sequenceDiagram
    actor Creator as Content Creator
    participant UI as React Dashboard
    participant API as FastAPI (Cloud Run)
    participant GCS as Cloud Storage
    participant PS as Pub/Sub
    participant Worker as Worker (Cloud Run)
    participant FS as Firestore
    participant Gemini

    Creator->>UI: Upload video + select target languages
    UI->>API: POST /projects/{id}/videos
    API->>GCS: Generate a signed upload URL
    API-->>UI: signed URL
    UI->>GCS: Upload directly (not through the API)
    UI->>API: POST /jobs (video_uri, target_langs)
    API->>FS: Create job status = "queued"
    API->>PS: Publish message {job_id}
    API-->>UI: 202 Accepted {job_id}

    PS->>Worker: Push message
    Worker->>FS: status = "transcribing"
    Worker->>Gemini: Extract dialogue + distinguish characters
    Worker->>FS: status = "translating"
    Worker->>Gemini: Translate + localize dialogue
    Worker->>FS: status = "safety_check"
    Worker->>Gemini: Cultural safety review for the target market
    Worker->>FS: status = "dubbing"
    Worker->>Gemini: Generate audio (Native Audio, per voice profile)
    Worker->>Worker: Mux audio into video (ffmpeg)
    Worker->>GCS: Save the dubbed video
    Worker->>FS: status = "ready_for_review"

    Creator->>UI: Open Review Dashboard (listen/edit each dialogue line)
    Creator->>API: POST /jobs/{id}/approve
    API->>FS: status = "published"
```

## 3. Agent architecture (details in `agent-pipeline.md`)

```mermaid
graph LR
    A[1. Ingestion &<br/>Transcription Agent] --> B[2. Character Voice<br/>Profiling Agent]
    B --> C[3. Translation &<br/>Transcreation Agent]
    C --> D[4. Cultural Safety<br/>Agent]
    D --> E[5. Dubbing / TTS<br/>Agent]
    E --> F[6. Audio-Video<br/>Mux Service]
    C --> G[7. Educational Content<br/>Agent]
    F --> H((Review Dashboard<br/>Human-in-the-loop))
    G --> H
```

## 4. Deployment view

```mermaid
graph TB
    subgraph "Cloud Run — xivi-api"
        A1[min instances: 0<br/>max instances: 10<br/>concurrency: 80]
    end
    subgraph "Cloud Run — xivi-worker"
        W1[min instances: 0<br/>max instances: 5<br/>concurrency: 1<br/>timeout: 3600s]
    end
    GH[GitHub Actions] -->|docker build + push| AR[(Artifact Registry)]
    AR -->|deploy| A1
    AR -->|deploy| W1
```

- `worker` is set to **concurrency: 1** because each job uses a lot of CPU/memory
  for audio/video processing (shouldn't process multiple jobs in parallel on
  the same instance)
- `timeout: 3600s` because Cloud Run supports up to 60 minutes/request — enough
  for a ~10-20 minute video (children's animated content is usually short, not
  a 1-hour video)

## 5. Key technical risks to watch for when building

| Risk | Mitigation |
|---|---|
| Cloud Run timeout on long videos | Split the video into scenes/chunks, process each chunk in parallel in the worker, stitch them back together at the end |
| Gemini Native Audio API may be rate-limited | Cache results by `(dialogue_line_id, target_lang, voice_profile_id)` in Redis, avoiding repeat calls on retry |
| Audio/video sync after dubbing | Keep the original timestamps from the transcription step, align the new audio to those timestamps instead of concatenating sequentially |
| Losing state if the worker crashes mid-run | Save a checkpoint after each agent step to the `jobs.current_stage` column in Postgres, the worker resumes from the last stage instead of starting over |
