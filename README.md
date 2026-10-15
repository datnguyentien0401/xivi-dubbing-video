# XIVI (ToonLingo) — Kids' Animation Localization Agent

> **Multilingual Agentic Video Dubbing & Localization Platform for Children's Content**  
> Powered by **Google Gemini** (Gemini 3.6 Flash multimodal/text, Gemini 3.1 Flash Native Audio TTS, and Gemini 3.8 Flash Voice Design) deployed on **Google Cloud Run** and **Firebase Hosting**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![React: 18](https://img.shields.io/badge/React-18-61DAFB.svg)](https://react.dev/)
[![TailwindCSS](https://img.shields.io/badge/TailwindCSS-3.4-38B2AC.svg)](https://tailwindcss.com/)
[![Google Cloud Run](https://img.shields.io/badge/Deployed_to-Cloud_Run-4285F4.svg)](https://cloud.google.com/run)
[![Firebase Hosting](https://img.shields.io/badge/Hosted_on-Firebase_Hosting-FFCA28.svg)](https://firebase.google.com/)

---

## 🌐 Live Environments & Demo Links

- **Web Application**: [https://toonlingo-app.web.app](https://toonlingo-app.web.app) (also available at [https://toonlingo-app.firebaseapp.com](https://toonlingo-app.firebaseapp.com))
- **Backend API Service**: [https://toonlingo-api-765982386020.asia-southeast1.run.app](https://toonlingo-api-765982386020.asia-southeast1.run.app)
- **Interactive OpenAPI (Swagger) Docs**: [https://toonlingo-api-765982386020.asia-southeast1.run.app/docs](https://toonlingo-api-765982386020.asia-southeast1.run.app/docs)
- **Interactive Architecture & Deployment Report**: [`docs/deployment-report.html`](docs/deployment-report.html)
- **Legacy Architecture Report**: [`docs/deployment-report-en.html`](docs/deployment-report-en.html)
- **Test & Performance Benchmark Report**: [`docs/performance-report.md`](docs/performance-report.md)

---

## 💡 What Makes XIVI Different?

Existing dubbing tools (e.g. standard generic TTS dubbers) fail on children's animated media: they flatten distinct characters into a generic narrator, produce clumsy literal translations devoid of child-friendly nuance, and ignore strict regional cultural taboos.

**XIVI solves these challenges with a 7-agent pipeline powered by Google Gemini:**

1. 🎭 **Consistent Character Voices Across Series**: Preserves distinct character voice profiles across multiple episodes using Gemini prebuilt Native Audio voices and **Gemini 3.8 Flash Voice Design** (custom voice synthesis from character descriptions).
2. 🌏 **Contextual Kids Dialogue Localization**: Adapts dialogue contextually to preserve humor, warmth, child-friendly idioms, and natural conversational cadence in target languages (Vietnamese, Indonesian, Japanese, Korean, Thai, Hindi, etc.) instead of clumsy literal machine translations.
3. 🛡️ **Market-Specific Cultural Safety Guardrails**: Contextual safety moderation checking target country rules (stored in PostgreSQL, dynamically customizable per country) with clear natural-language rationale for flags.
4. 📚 **Educational Flashcards & Interactive Quizzes**: Agent 7 automatically generates bilingual vocabulary flashcards and comprehension quiz items directly from the dubbed dialogue.
5. 🎬 **In-Browser Canva-like Timeline Editor**: Built-in multi-track timeline video/audio editor with WaveSurfer.js waveform rendering, drag-and-drop timing, audio trimming, volume adjustment, autosave, and server-side FFmpeg remuxing (`specs/002-video-editor`).
6. ⚡ **Script-First Ingestion Workflow**: Upload locked scripts (CSV/text) to run translation and dubbing in parallel with animation production, before a video even exists, with AI-generated duration benchmarks (`specs/003-script-first-ingestion`).
7. 🔁 **Fault-Tolerant Checkpoint & Resume**: Long-running jobs persist their `current_stage` in Cloud SQL after every step. If worker execution is interrupted, the job resumes from the exact stage without repeating costly Gemini calls.

---

## 🏛️ System Architecture

XIVI separates concerns between interactive user traffic and compute-intensive pipeline processing:

```mermaid
graph TD
    Client["Browser / Client<br/>(React 18 + Vite + Zustand)"]
    FirebaseHosting["Firebase Hosting<br/>toonlingo-app.web.app"]
    
    subgraph GCP["Google Cloud Platform (asia-southeast1)"]
        API["Cloud Run: toonlingo-api<br/>(FastAPI Orchestrator, &lt;1s response)"]
        Worker["Cloud Run: toonlingo-worker<br/>(Agent Pipeline + FFmpeg, 2 vCPU / 2Gi)"]
        PubSub["Cloud Pub/Sub<br/>Topic: agent-events"]
        CloudSQL[("Cloud SQL PostgreSQL<br/>toonlingo-pg")]
        GCS[("Cloud Storage<br/>toonlingo-app-media")]
        Firestore[("Cloud Firestore<br/>Realtime State &amp; Progress")]
        Redis[("Redis Cache<br/>Gemini Result Deduplication")]
        Secrets["Secret Manager<br/>(API keys, DB &amp; Redis URLs)"]
    end

    Gemini["Google Gemini API<br/>• Gemini 3.6 Flash (Text / Vision / Multimodal)<br/>• Gemini 3.1 Flash TTS (Native Audio)<br/>• Gemini 3.8 Flash Voice Design"]

    Client --> FirebaseHosting
    Client -->|REST + Bearer Token| API
    Client -->|Direct Media Streaming &amp; Upload| GCS
    Client -->|onSnapshot Realtime Listener| Firestore

    API -->|CRUD &amp; Timeline State| CloudSQL
    API -->|Signed URLs| GCS
    API -->|Publish Job Event| PubSub
    
    PubSub -->|OIDC Push Subscription| Worker
    Worker -->|Execute Pipeline &amp; Checkpoints| CloudSQL
    Worker -->|Status Updates| Firestore
    Worker -->|Download / Upload Muxed Media| GCS
    Worker -->|Cache Hits / Sets| Redis
    Worker -->|Inference &amp; Audio Generation| Gemini
    Secrets -.->|Injected on Startup| API
    Secrets -.->|Injected on Startup| Worker
```

### The 7-Stage Worker Pipeline

| Stage | Agent / Service | Gemini Model / Tool | Responsibility |
|---|---|---|---|
| **1. Ingestion** | `IngestionAgent` | `gemini-3.6-flash` | Extracts dialogue lines, sub-second timestamps, and speaker diarization from video audio. In script-first jobs, parses text and calculates benchmark timing. |
| **2. Voice Profiling** | `VoiceProfilingAgent` | `gemini-3.6-flash` & `gemini-3.8-flash-tts` | Identifies character personas, matches existing character voices in the project registry, or designs new voices with custom timbre/pitch. |
| **3. Translation** | `TranslationAgent` | `gemini-3.6-flash` | Translates dialogue contextually with kid-friendly tone, humor, and natural conversational cadence in target languages. |
| **4. Cultural Safety** | `SafetyAgent` | `gemini-3.6-flash` | Cross-checks localized lines against DB rules for the target market; outputs severity score (`safe`, `low`, `medium`, `high`) and actionable rationale. |
| **5. Dubbing** | `DubbingAgent` | `gemini-3.1-flash-tts-preview` | Generates speech audio with prebuilt or custom voices; applies pitch/tempo corrections to fit video line windows. |
| **Muxing** | `MuxService` | `ffmpeg` | Stitches synthesized dialogue with original background audio and video, writing faststart `moov` atom for instant streaming. |
| **6. Educational** | `EducationalAgent` | `gemini-3.6-flash` | Extracts key vocabulary and interactive multiple-choice quiz items saved to `vocabulary_items` and `quiz_items`. |
| **Timeline Export** | `EditorExportService` | `ffmpeg` | Executes timeline edits (cuts, trims, volume gain, offset adjustments) directly from user editor state. |

---

## 🚀 Running Locally

You can run the entire stack locally with **Docker Compose** (recommended) or manually on your host machine.

### Option A: Quickstart with Docker Compose (Recommended)

The local compose stack boots PostgreSQL, Redis, the FastAPI backend, the Worker service, and the React frontend. It includes local media storage and Pub/Sub bypass for instant local testing without GCP credentials.

```bash
# 1. Clone repository
git clone https://github.com/phamhiep1105/xivi-dubbing-video.git
cd xivi-dubbing-video

# 2. Configure environment
cp .env.example .env
# Optional: Add your GEMINI_API_KEY to .env to enable real Gemini inference

# 3. Start all services
docker compose up --build
```

**Local Ports & Endpoints:**
- 🖥️ **Frontend**: [http://localhost:5183](http://localhost:5183)
- 🔌 **API Service**: [http://localhost:8010](http://localhost:8010) (OpenAPI Docs: [http://localhost:8010/docs](http://localhost:8010/docs))
- ⚙️ **Worker Service**: [http://localhost:8011](http://localhost:8011)
- 🗄️ **PostgreSQL**: `localhost:5433` (User: `xivi`, Password: `xivi`, Database: `xivi`)
- 📦 **Redis**: `localhost:6380`

*Note: Database migrations run automatically on startup via the `migrate` container.*

---

### Option B: Running Services Directly on Host

#### 1. Prerequisites
- **Python**: `>= 3.12`
- **Node.js**: `>= 20.x`
- **FFmpeg**: Installed and accessible on `$PATH` (`brew install ffmpeg` on macOS)
- **PostgreSQL 16** & **Redis 7** running locally

#### 2. Backend Setup
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Seed cultural safety rules (Vietnam, Indonesia, Philippines, etc.)
python -m scripts.seed_cultural_safety_rules

# Start API Orchestrator (port 8010)
uvicorn api.main:app --host 0.0.0.0 --port 8010 --reload

# In another terminal: Start Worker Service (port 8011)
source .venv/bin/activate
uvicorn worker.main:app --host 0.0.0.0 --port 8011 --reload
```

#### 3. Frontend Setup
```bash
cd frontend
npm install
npm run dev
```

---

## 🧪 Testing & Verification

The project includes an extensive automated test suite with **over 300 unit and integration tests** covering all API routers, pipeline agents, and services.

### Running Backend Tests
```bash
cd backend
DATABASE_URL=postgresql+asyncpg://xivi:xivi@localhost:5433/xivi \
GOOGLE_CLOUD_PROJECT=test-project \
REDIS_URL=redis://localhost:6380 \
GCS_BUCKET_NAME=test-bucket \
FIREBASE_PROJECT_ID=test-project \
USE_LOCAL_PUBSUB=true \
python -m pytest
```

> **Data Safety in Tests**: Tests utilize SQLAlchemy outermost savepoint transaction rollbacks (`backend/conftest.py`). Every database commit inside application code operates on an isolated savepoint that is rolled back upon test completion, preventing test artifacts from leaking into the database.

### Running Frontend Checks
```bash
cd frontend
npm run lint      # ESLint checks
npm run build     # TypeScript compilation (tsc -b) & Vite bundle validation
```

### Performance & Load Testing
- **Pipeline Agent Latency Benchmark**:
  ```bash
  cd backend
  python -m scripts.benchmark_pipeline <job_id>
  ```
- **API High-Concurrency Load Tester**:
  ```bash
  cd backend
  python -m scripts.load_test_api GET /projects -c 50 -n 200
  ```

---

## 🚢 Deployment & CI/CD

### 1. Automated GitHub Actions CI/CD
Every push to `main` triggering modifications in `backend/`, `frontend/`, or `infra/` runs `.github/workflows/deploy.yml`:
1. Authenticates via **GCP Workload Identity Federation** (zero static credentials).
2. Builds and pushes Docker images for `Dockerfile.api` and `Dockerfile.worker` to Artifact Registry.
3. Automatically executes database migrations via a one-off Cloud Run job (`toonlingo-migrate`).
4. Updates both Cloud Run services with zero downtime.
5. Injects the live API URL and builds the frontend production bundle, deploying directly to **Firebase Hosting**.

### 2. Manual Provisioning Script
For first-time GCP project bootstrapping:
```bash
bash infra/deploy.sh <GCP_PROJECT_ID> asia-southeast1
```
This script enables all required GCP APIs, configures GCS buckets with video streaming CORS headers (`Range`), provisions the Cloud SQL PostgreSQL instance, configures IAM service accounts, and connects Cloud Pub/Sub push subscriptions.

---

## 📁 Repository Structure

```
xivi-dubbing-video/
├── AGENTS.md                  # Project constitution & global engineering rules
├── GEMINI.md                  # Gemini CLI specific mandates & skills
├── docker-compose.yml         # Full local development stack (Postgres, Redis, API, Worker, UI)
├── .env.example               # Template environment configuration
├── firebase.json              # Firebase Hosting & Firestore configuration
│
├── .github/workflows/         # CI/CD deployment pipeline (Workload Identity Federation)
│   └── deploy.yml
│
├── .gemini/                   # Agent skills, subagents, and slash commands
│   ├── agents/                # Specialized subagents (code-reviewer, gemini-prompt-reviewer)
│   ├── commands/              # Spec-Kit commands (/speckit.specify, /speckit.plan, etc.)
│   └── skills/                # Reusable skills (react-dashboard-page, gemini-pipeline-agent)
│
├── specs/                     # Specification-driven development artifacts
│   ├── 001-dubbing-pipeline/  # Core dubbing & localization pipeline spec & tasks
│   ├── 002-video-editor/      # Canva-like video & audio timeline editor spec & tasks
│   └── 003-script-first-ingestion/ # Script-first parallel localization workflow spec & tasks
│
├── docs/                      # Technical specifications & benchmark reports
│   ├── architecture.md        # System diagrams & architecture decisions
│   ├── agent-pipeline.md      # Detailed 7-agent pipeline design & data contracts
│   ├── backend-spec.md        # FastAPI & Worker internal technical specification
│   ├── frontend-spec.md       # React studio dashboard UX & state management spec
│   ├── database-schema.md     # Relational schema (PostgreSQL) & Firestore models
│   ├── performance-report.md  # Test suite audit & latency benchmark findings
│   ├── deployment-report.html # Interactive visual architecture & deployment report
│   └── deployment-report-en.html # Legacy deployment guide
│
├── backend/                   # Python 3.12 Backend Services
│   ├── alembic/               # Database migration versions
│   ├── api/                   # FastAPI Orchestrator Service (Cloud Run Service #1)
│   │   ├── core/              # Config, Firebase Auth verification, Local PubSub
│   │   ├── db/                # SQLAlchemy models & async session management
│   │   ├── routers/           # projects, jobs, dialogue_lines, characters, local_storage
│   │   └── schemas/           # Pydantic v2 validation models
│   ├── worker/                # Long-Running Pipeline Service (Cloud Run Service #2)
│   │   ├── agents/            # Ingestion, VoiceProfiling, Translation, Safety, Dubbing, Educational
│   │   ├── services/          # GeminiClient, GCSClient, MuxService, EditorExportService, RedisClient
│   │   ├── models/            # PipelineContext & internal dataclasses
│   │   ├── pipeline.py        # Checkpoint-resilient pipeline runner
│   │   └── main.py            # Pub/Sub push endpoint & background task runner
│   ├── shared/                # Gemini prompt repository (reviewed against GenAI rubric)
│   │   └── prompts.py
│   └── scripts/               # Benchmark, load test, and seed scripts
│
├── frontend/                  # React 18 + Vite + TypeScript Studio Dashboard
│   ├── src/
│   │   ├── components/        # UI components, layout, voice pickers, and video players
│   │   │   └── editor/        # Timeline, TrackRow, ClipBlock, Playhead, WaveformClip
│   │   ├── pages/             # Dashboard, VideoEditor, ReviewDashboard, VoiceManager, EduPreview
│   │   ├── stores/            # Zustand state stores (timelineStore, etc.)
│   │   ├── hooks/             # TanStack Query custom hooks & mutations
│   │   └── lib/               # Firebase SDK client, API client, utilities
│   └── package.json
│
└── infra/
    └── deploy.sh              # One-command GCP Cloud Run & Pub/Sub provisioning script
```

---

## 🛠️ Tech Stack Reference

| Category | Technologies |
|---|---|
| **AI & LLM Core** | **Google Gemini** (`gemini-3.6-flash`, `gemini-3.1-flash-tts-preview`, `gemini-3.8-flash-tts`) via `google-genai` SDK |
| **Backend Services** | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (Async), Alembic, FFmpeg |
| **Frontend Web App** | React 18, TypeScript, Vite, Tailwind CSS, Radix UI primitives, WaveSurfer.js |
| **State & Data Fetching** | TanStack Query v5, Zustand, React Hook Form, Zod |
| **Cloud Infrastructure** | Google Cloud Run, Cloud Pub/Sub, Cloud Storage, Cloud Firestore, Cloud SQL (PostgreSQL 16) |
| **Authentication & CDN** | Firebase Authentication, Firebase Hosting |
| **Caching & Acceleration** | Redis (Memorystore / Upstash), FFmpeg faststart streaming optimization |

---

## 📄 License & Contributing

- Refer to [`AGENTS.md`](AGENTS.md) and [`GEMINI.md`](GEMINI.md) for architectural constraints, code style standards, and agent skills before contributing.
- Code released under the [MIT License](LICENSE).
