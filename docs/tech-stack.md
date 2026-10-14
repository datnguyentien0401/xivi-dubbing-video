# Tech Stack — full table & rationale

## 1. Summary table

| Layer | Technology | Required/Optional |
|---|---|---|
| Frontend framework | React 18 + Vite + TypeScript | Required |
| Styling | TailwindCSS + shadcn/ui | Required |
| Data fetching | TanStack Query | Required |
| State | Zustand | Required |
| Backend framework | Python 3.12 + FastAPI | Required |
| ORM | SQLAlchemy 2.0 async + Alembic | Required |
| AI models | Gemini 2.5/3.5 (text, vision) + Gemini Native Audio | Required (core of the product) |
| Fallback Text-to-Speech | Cloud Text-to-Speech (Chirp 3) | Optional — fallback if Native Audio is rate-limited |
| Relational database | Cloud SQL (PostgreSQL 16) | Required |
| Realtime state | Firestore | Required |
| Cache | Memorystore for Redis | Required |
| Message bus | Cloud Pub/Sub | Required (replaces Kafka — see section 2) |
| Object storage | Google Cloud Storage | Required |
| Auth | Firebase Authentication | Required |
| Video/audio processing | ffmpeg (via `ffmpeg-python`) | Required |
| CI/CD | GitHub Actions → Cloud Build/Artifact Registry | Required |
| Compute | Cloud Run (2 services: api, worker) | Required |
| Observability | Cloud Logging + Cloud Monitoring | Nice to have, not required for the demo |
| IaC | Terraform | Optional — using the `gcloud` CLI manually is fine too |

## 2. Kafka or Pub/Sub? — Recommendation: **Pub/Sub**

Kafka was mentioned in the original requirements, so the tradeoff deserves a
clear explanation:

| | Apache Kafka (self-hosted) | Google Cloud Pub/Sub |
|---|---|---|
| Operations | Must manage the cluster yourself (or use Confluent Cloud — costs money, takes setup time) | Fully managed, nothing to operate |
| Cloud Run integration | Needs an extra hand-written client/consumer polling loop, or Kafka Connect | Native push subscription → calls the Cloud Run endpoint's HTTP directly, no separate consumer code needed |
| Setup time | At least 2-3 days to stand up + debug a cluster | Create a topic + subscription in a few minutes with the `gcloud` CLI |

**Conclusion**: using Kafka here would be **over-engineering** — spending time
operating infrastructure instead of building the core feature (the agent
pipeline). Pub/Sub does exactly what's needed (coordinating messages between
API and Worker) at near-zero operational cost. If the product scales to
millions of videos/day in the future with complex message-replay requirements,
Kafka could be reconsidered then — but that's not the problem being solved by
a 3-week demo.

## 3. What Redis is specifically used for (not "just because")

Redis (Memorystore) serves 2 clear purposes, not added just to "round out the
tech stack":

1. **Caching Gemini call results** keyed by `hash(text + target_lang + mode)` —
   avoiding repeated API calls that cost money/time when the worker retries
   after a network error, or when 2 different videos share a common line of
   dialogue (e.g. a series' recurring opening greeting)
2. **Caching consistently-translated glossary terms** within a project —
   ensuring the same character name/place name is translated the same way
   throughout all episodes, instead of Gemini translating it differently on
   each call

Without Redis: the system would still work, but **Gemini API costs would be
higher** and there would be a risk of inconsistent translation of proper nouns
across episodes — this is the real reason to keep Redis in the stack, not just
to "look like a complete tech stack".

## 4. Firestore vs. writing a custom WebSocket server

A WebSocket server (FastAPI supports this natively) could be written to push
job status to the UI, but Firestore's realtime listener was chosen instead
because:
- No need to manage WebSocket connections, reconnect logic, or horizontal
  scaling yourself
- The Firebase JS SDK on the frontend already has a very clean listener API
  (`onSnapshot`)
- Saves 1-2 days of effort on a supporting feature, leaving more time for the
  agent pipeline (the most heavily weighted scoring area)
