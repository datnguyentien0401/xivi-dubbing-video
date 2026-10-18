# XIVI Studio — Executive Solution & Infrastructure Summary
> **Architecture & Deployment Summary**  
> *A focused, high-signal technical executive summary.*

---

## 1. Problem Statement & Autonomous Agentic Solution

Commercial off-the-shelf automated dubbing tools fail when applied to children's animation: they flatten expressive character personalities into a single generic narrator, produce clumsy literal machine translations that lack kid-friendly warmth, and ignore strict regional cultural taboos across JAPAC markets.

**XIVI Studio** re-engineers localization into a collaborative multi-agent pipeline using **Google Gemini** as the single intelligence core:
* **Series-Wide Voice Consistency**: Every character is assigned a persistent `character_id` and acoustic profile in PostgreSQL, powered by Gemini 3.8 Flash Voice Design and native audio prebuilt voices so voices never drift across episodes.
* **Contextual Kids Localization**: Dialogue adapts humor, warmth, and child-friendly idioms in target languages rather than translating literally.
* **Cultural Safety Guardrails**: Dialogue is screened against extensible database rules (`cultural_safety_rules` table) per target country with natural-language rationales.
* **Mandatory Human Review Gate**: No AI agent is permitted to auto-publish content destined for young audiences. Every episode requires explicit review and approval.

---

## 2. End-to-End Orchestrated Event Flow

```
[ Creator Web App (Firebase Hosting) ]
       │
       ├─ (1) Stream video direct to Cloud Storage (V4 Signed URLs)
       ├─ (2) Submit POST /jobs to API Orchestrator
       │
       ▼
[ Cloud Run: api ] ──(Record queued state)──► [ Cloud SQL Postgres ]
       │                                             │
       ├─ (Init progress 0%) ──► [ Cloud Firestore ]  │ (Checkpoints)
       │                              ▲              │
       ▼                              │ (onSnapshot) │
[ Cloud Pub/Sub: agent-events ]       │              │
       │                              │              │
       │ (Push via IAM OIDC Token)    │              │
       ▼                              │              │
[ Cloud Run: worker ] ────────────────┴──────────────┘
       │
       ├─► Redis: Distributed locks (TTL 1800s) & Gemini translation cache (24h)
       ├─► Gemini API: 3.6 Flash (Text/Vision), 3.1 TTS (Native Audio), 3.8 Voice Design
       └─► FFmpeg Engine: Audio mixing, volume ducking, and H.264 video remuxing
```

---

## 3. Infrastructure & Deployment Implementation

### 1. 🌐 Firebase Hosting & Firebase Auth
* **Global Edge CDN**: Distributes the compiled React 18 SPA bundle across Google edge locations with single-page rewrite rules (`** -> /index.html`) for instantaneous global loading.
* **Token-Based Auth**: Authenticates creators via Google OAuth; FastAPI verifies Firebase Auth JWT ID tokens server-side on every request via `firebase_admin.auth.verify_id_token`.

### 2. 🔥 Cloud Firestore (Realtime State Streaming)
* **Zero-WebSocket Streaming**: The client dashboard uses Firestore's client-side `onSnapshot` listener to stream live pipeline stage transitions and progress percentages (0% to 100%) without maintaining WebSocket server clusters.
* **Separation of Concerns**: Firestore exclusively stores ephemeral runtime progress. Core transactional data, permissions, dialogue lines, and checkpoint states reside permanently in Cloud SQL PostgreSQL.

### 3. 🚀 Google Cloud Run (Decoupled Microservices)
* **API Service (`api`)**: Lightweight, fast-scaling (0–10 instances) HTTP gateway handling CRUD, signed upload URLs, and job triggering with sub-50ms latency.
* **Worker Service (`worker`)**: Allocated **2 vCPU, 2 GiB RAM**, 3600s timeout, and `concurrency=1` to guarantee CPU-intensive FFmpeg audio/video remuxing runs in complete isolation without out-of-memory errors.
* **Cloud Run Jobs**: Executes short-lived tasks running `alembic upgrade head` migrations deterministically before new service revisions accept traffic.

### 4. 📨 Cloud Pub/Sub (Event Streaming & Decoupling)
* **Asynchronous Decoupling**: The API publishes events (`{"type": "job", "job_id": "..."}`) and immediately returns `202 Accepted`, freeing HTTP threads from blocking on long tasks.
* **Secured Push Subscriptions**: Cloud Pub/Sub delivers messages to private worker endpoints using Google IAM OIDC Identity Tokens verified against `PUBSUB_PUSH_AUDIENCE`.

### 5. ⚡ Redis (Distributed Locking & Gemini Cache)
* **Distributed Locks**: Manages `pubsub_lock:{job_id}` (TTL 1800s) to prevent duplicate execution during Pub/Sub redeliveries or concurrent race conditions.
* **Gemini Result Caching**: Caches translation responses under `translate:{hash}:{lang}` with a 24-hour TTL, saving substantial API quota and reducing latency on repeated phrases.

### 6. 🧠 Google Gemini AI Core (Multi-Model Pipeline)
* **`gemini-3.6-flash`**: Powers transcription diarization, contextual translation, cultural safety assessment with reasoning, and bilingual vocabulary/quiz generation.
* **`gemini-3.1-flash-tts-preview`**: Synthesizes expressive native audio speech matching original dialogue timing.
* **`gemini-3.8-flash-tts`**: Designs bespoke character voices directly from personality descriptions.
* **3-Tier Fallback**: Voice Design ➔ 12 Prebuilt Native Voices ➔ Deterministic Hash Fallback (`hash(character_id) % 12`) ensures zero dubbing pipeline failures.
