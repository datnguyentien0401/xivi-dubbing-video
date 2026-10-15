# XIVI (ToonLingo) — Executive Technical Architecture & Deployment Report
> **Executive Summary & Deployment Architecture**  
> *Condensed executive version derived from `docs/deployment-report.html` (comprehensive 1900+ lines).*

---

## 1. Solution Overview & Core Engineering Differentiators

**XIVI (ToonLingo)** is an autonomous Agentic AI localization platform that automates dialogue translation, cultural adaptation, and native speech dubbing for children's animated video across JAPAC markets (Vietnam, Indonesia, Japan, etc.). Powered by the **Google Gemini** ecosystem as the unified intelligence core, the system avoids training custom CV/NLP models from scratch.

### Key Engineering Differentiators:
1. **Series-Wide Voice Consistency**: Every character is assigned a persistent `character_id` profile in PostgreSQL. Timbre and pitch are preserved using **Gemini 3.8 Flash Voice Design** and **Gemini Native Voices**, guaranteeing character voice consistency across all episodes.
2. **Contextual Kids Dialogue Localization**: Instead of clumsy literal machine translations, specialized prompts instruct Gemini to adapt humor, child-friendly idioms, warmth, and natural conversational cadence tailored for young audiences in target languages.
3. **Dynamic Cultural Safety Guardrails**: Dialogue is automatically evaluated against extensible, database-backed rules (`cultural_safety_rules` table per country). Gemini generates severity scores (Safe, Low, Medium, High) accompanied by natural-language rationales for human editors.
4. **Bilingual Learning & Quiz Generation (Agent 7)**: Running asynchronously off the critical video path, Agent 7 extracts 8–12 bilingual vocabulary flashcards and interactive multiple-choice quiz questions directly from the dubbed dialogue.
5. **In-Browser Multi-Track Timeline Editor (Spec 002)**: An embedded Canva-style WaveSurfer.js waveform editor allows visual lip-sync adjustments, audio trimming, volume balancing, debounced autosave to JSONB, and server-side non-destructive FFmpeg remuxing.
6. **Script-First Parallel Workflow (Spec 003)**: Studios can initiate translation and dubbing directly from locked script files (CSV/text) before animation renders are finished, using AI audio duration benchmarks. The final video is attached later via `POST /jobs/{id}/attach-video`.
7. **Mandatory Human Review Gate**: In strict adherence to our safety constitution, no AI agent is permitted to auto-publish content for children. Creators must review and explicitly approve content before release.

---

## 2. Leveraging Firebase, Cloud Firestore, Cloud Run & GCP Services

The platform utilizes Google Cloud's **Serverless Managed Services** to optimize performance, minimize operational overhead, and deliver realtime reactivity:

### 1. Firebase Hosting & Firebase Authentication
* **Firebase Hosting (Global Edge CDN):** Distributes the precompiled React 18 SPA (`toonlingo-app.web.app`) across Google edge points of presence, configured with single-page rewrite rules (`** -> /index.html`).
* **Firebase Authentication:** Manages creator identity, issuing secure JWT ID tokens verified server-side in FastAPI via `firebase_admin.auth.verify_id_token`.

### 2. Cloud Firestore (Realtime State Streaming)
* **Zero-WebSocket Streaming:** Replaces expensive, failure-prone WebSocket clusters with Firestore's client-side `onSnapshot` listeners. The React dashboard reflects pipeline stage transitions and progress percentages (0% to 100%) in real time as the worker updates state.
* **Separation of Concerns:** Firestore exclusively stores ephemeral runtime progress. Core transactional data, permissions, dialogue lines, and checkpoint states reside permanently in Cloud SQL PostgreSQL.

### 3. Google Cloud Run (Decoupled Microservices)
The system decouples interactive client traffic from long-running media processing:
* **`toonlingo-api` (API Orchestrator Service):**
  * Profile: Lightweight, auto-scales from 0 to 10 instances (scale-to-zero), p50 HTTP latency < 50ms.
  * Role: Handles REST requests, issues V4 Signed Upload URLs, persists initial job records, and enqueues events.
* **`toonlingo-worker` (Worker Pipeline & FFmpeg Engine Service):**
  * Profile: Allocated **2 vCPU, 2 GiB RAM**, 3600s maximum timeout, and `concurrency = 1`.
  * Role: Guarantees memory-intensive FFmpeg audio/video remuxing and model inference run in complete isolation without Out-Of-Memory (OOM) risks.
* **Cloud Run Jobs (`toonlingo-migrate`):** Executes short-lived tasks running `alembic upgrade head` to guarantee schema migrations succeed before new service revisions accept traffic.

### 4. Cloud Pub/Sub, Cloud Storage & Cloud SQL
* **Cloud Pub/Sub (`agent-events`):** Acts as an asynchronous buffer queue. The API publishes `{job_id}` and immediately returns `202 Accepted`. Pub/Sub invokes the Worker via an HTTP Push Subscription secured with Google IAM OIDC identity tokens.
* **Cloud Storage (V4 Resumable Signed URLs):** Clients stream large video files directly to `toonlingo-app-media` with time-bounded signed URLs (15-minute expiration), bypassing API server memory and network bandwidth.
* **Cloud SQL PostgreSQL 16 (`toonlingo-pg`):** Delivers ACID reliability. The Worker persists a **transactional checkpoint** to `jobs.current_stage` before each stage transition. If a container is preempted mid-pipeline, Pub/Sub redelivers the event and the Worker resumes from the exact stage without re-running completed Gemini steps.

---

## 3. End-to-End Orchestration Architecture

```
[ Content Creator Web Browser ]
       │
       ├─ (1) Load SPA Bundle from Firebase Hosting (Global Edge CDN)
       ├─ (2) Fetch V4 Signed URL from API -> Stream video directly to Cloud Storage (GCS)
       ├─ (3) Submit POST /jobs to start localization pipeline
       │
       ▼
[ Cloud Run: toonlingo-api ] ──(Record Queued Job)────────► [ Cloud SQL Postgres ]
       │                                                          │
       ├─ (Set Initial Progress 0%) ──► [ Cloud Firestore ]        │
       │                                     ▲                    │
       │                                     │ (onSnapshot        │
       │                                     │  realtime sync)    │
       ▼                                     │                    │
[ Cloud Pub/Sub: agent-events ]              │                    │
       │                                     │                    │
       │ (OIDC Push)                         │                    │
       ▼                                     │                    │
[ Cloud Run: toonlingo-worker ] ─────────────┴─ (Stage Checkpoints)┘
       │
       ├─► Agent 1: Audio Diarization & Timestamps (Gemini 3.6 Flash)
       ├─► Agent 2: Voice Profiling & Design (Gemini 3.8 Flash TTS / Native Voices)
       ├─► Agent 3: Translation & Contextual Localization (Gemini 3.6 Flash)
       ├─► Agent 4: Cultural Safety Moderation (Gemini 3.6 Flash + DB Rules)
       ├─► Agent 5: Native Audio TTS Dubbing (Gemini 3.1 Flash TTS Preview)
       ├─► Stage 6: FFmpeg Demux/Remux Engine (Faststart MP4)
       └─► Agent 7: Bilingual Flashcards & Quiz Generator (Gemini 3.6 Flash)
```

---

## 4. Google Gemini Model Selection Matrix

| Gemini Model | Pipeline Stage | Technical Selection Rationale |
| :--- | :--- | :--- |
| **`gemini-3.6-flash`** | Ingestion Diarization, Translation, Cultural Safety, Educational Content | Sub-second latency, massive multimodal context window, strict JSON schema adherence. |
| **`gemini-3.1-flash-tts-preview`** | High-Frequency Dialogue Native Audio TTS | Direct audio waveform generation using prebuilt expressive voices without restrictive daily quotas. |
| **`gemini-3.8-flash-tts`** | Custom Character Voice Design | Synthesizes custom voices directly from character personality descriptions via `client.voices.create(type="prompted")`. |

### 3-Tier Voice Fallback Hierarchy:
1. **Tier 1 (Gemini 3.8 Voice Design):** Synthesizes bespoke voices from personality prompts. If Google's safety filter flags the generated voice (e.g. sounding like a minor), it gracefully degrades to Tier 2.
2. **Tier 2 (Prebuilt Native Voice):** Matches the character with one of 12 prebuilt voices based on gender, pitch, and energy.
3. **Tier 3 (Deterministic Hash Fallback):** Computes `hash(character_id) % 12` to ensure the character consistently uses the exact same voice across all episodes.

---

## 5. Security Governance & Performance Benchmarks

* **Zero Static Secret Leaks:** CI/CD deployment via GitHub Actions authenticates with GCP using **Workload Identity Federation (WIF)** over OIDC, eliminating static Service Account JSON keys from repository secrets.
* **Isolated Testing Environment:** Over **300 automated tests** execute inside nested database savepoint transactions (`conftest.py`), rolling back completely after each run without test data pollution.
* **Load Test Latency & Reliability:**
  * `GET /projects`: p50 of **24ms** (44.2 req/s) and **72ms** under 50 concurrent connections (443.6 req/s).
  * `GET /jobs/{id}/dialogue-lines`: p50 of **12ms** (343.7 req/s).
  * Overall load test error rate: **0.00% (0 errors across 450 test requests)**.
