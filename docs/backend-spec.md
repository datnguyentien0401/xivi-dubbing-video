# Backend Spec — Python Services

## 1. Tech stack

| Category | Choice | Reason |
|---|---|---|
| Framework | FastAPI | Native async, auto-generates OpenAPI docs, type-safe with Pydantic |
| Runtime | Python 3.12 | Better async performance than older versions, full type hint support |
| ORM | SQLAlchemy 2.0 (async) + Alembic | Clear migrations, type-safe queries |
| Validation | Pydantic v2 | Validates request/response, shares schemas between API and Worker |
| Gemini SDK | `google-genai` (official Python SDK) | Calls Gemini text/vision/native audio |
| Video/Audio processing | `ffmpeg-python` (wrapper around the ffmpeg binary) | Muxes audio into video, cuts/extracts audio tracks |
| Task orchestration | Cloud Pub/Sub push subscription (no Celery) | Simpler for Cloud Run, no need to self-manage a broker (see `tech-stack.md`) |
| Auth verification | Firebase Admin SDK | Verifies the ID token sent up by the frontend |

## 2. Two separate services

### `backend/api` — Orchestrator Service (Cloud Run, always receives HTTP)

Responsible for: receiving requests from the frontend, CRUD operations, generating
signed URLs, publishing messages to Pub/Sub. **Never** runs any Gemini agent
directly (to avoid blocking the request for a long time).

**Main endpoints:**

| Method | Path | Description |
|---|---|---|
| POST | `/projects` | Create a new project |
| GET | `/projects` | List the user's projects |
| POST | `/projects/{id}/upload-url` | Generate a signed URL so the frontend can upload the video directly to GCS |
| POST | `/projects/{project_id}/script-jobs` | Script-first job creation (specs/003-script-first-ingestion, US1): create 1 `Job` per target language directly from an uploaded text script, no source video required yet — see section 6 for the script's CSV format |
| DELETE | `/projects/{id}` | Delete a project + all related data in the DB (video, job, dialogue line, character, vocabulary) — does not yet delete the files on GCS |
| POST | `/jobs` | Create a processing job (video_uri, target_langs[]) → publish to Pub/Sub |
| GET | `/jobs/{id}` | Get a job's status (fallback for when the Firestore listener fails) |
| GET | `/jobs/{id}/dialogue-lines` | List of dialogue lines + status for the Review Dashboard to display |
| GET | `/jobs/{id}/characters` | List of characters belonging to the job's project — for the voice-selection dropdown in the Review Dashboard |
| PATCH | `/dialogue-lines/{id}` | Edit the translated text and/or change the character (`character_id`) → triggers a re-dub for just that line (rewinds `job.current_stage` back to right before "dubbed" then republishes, reusing the existing pipeline — see `worker/pipeline.py::_STAGE_ORDER`) |
| POST | `/jobs/{id}/approve` | Approve everything, transition status to "published" — works for a script-first job with no video yet (audio-only branch) |
| POST | `/jobs/{id}/attach-video` (specs/003-script-first-ingestion, US5) | Link a real video onto a script-first job after the fact; runs ONLY the mux step against the already-dubbed audio, does not re-run translation/safety/dubbing |
| GET | `/jobs/{id}/dubbed-audio-bundle` (specs/003-script-first-ingestion, FR-008) | Zips every dubbed dialogue line's audio (named by `segment_code`), returns a signed URL — the deliverable for a script-first job before a video is ever attached |
| GET | `/jobs/{id}/timeline` | Video Editor (specs/002-video-editor): returns the saved `timeline_edits`, or builds a default snapshot from `dialogue_lines` if the job has never had its editor opened. 409 if `current_stage != "ready_for_review"` |
| PATCH | `/jobs/{id}/timeline` | Video Editor: debounced autosave, overwrites `timeline_edits` entirely (only UPDATEs this specific column, not via an ORM attribute on `job`) |
| POST | `/jobs/{id}/export` | Video Editor: transitions `current_stage` to "exporting" then publishes a `type="job_export"` message for the worker to render the exported version from `timeline_edits` (see below) |
| GET | `/characters?project_id=` | List of characters + voice profiles |
| POST | `/characters` | Manually create a character (with no voice sample yet) — used when adding a voice by uploading audio manually, independent of the automatic diarization result |
| PATCH | `/characters/{id}` | Rename a character, requires re-dubbing the voice sample |
| POST | `/characters/{id}/voice-sample-upload-url` | Generate a signed URL so the frontend can upload a voice sample directly to GCS (same pattern as `/projects/{id}/upload-url`) |
| POST | `/characters/{id}/classify-voice-sample` | Triggers the worker to listen to the just-uploaded sample and pick the closest TTS voice (asynchronous) |
| GET | `/characters/voice-options` (specs/003-script-first-ingestion, US2) | The 12 prebuilt TTS voices, for a human to pick from directly — no audio sample needed |
| PUT | `/characters/{id}/voice` (specs/003-script-first-ingestion, US2) | Manually assign one of those prebuilt voices to a character, for a script-first job where there's no voice sample for `VoiceProfilingAgent` to listen to |

### `backend/worker` — Agent Pipeline Runner (Cloud Run, Pub/Sub push subscription)

Responsible for: running the 6 agents in sequence (see `agent-pipeline.md`),
updating status in Postgres + Firestore after each step.

**Structure:**

```
backend/worker/
├── main.py                     # receives the Pub/Sub push (a dedicated HTTP endpoint, OIDC token verified)
├── pipeline.py                  # orchestrates running the agents in sequence, handles checkpoint/resume
├── agents/
│   ├── base.py                  # shared interface: run(context) -> context
│   ├── ingestion_agent.py
│   ├── voice_profiling_agent.py
│   ├── translation_agent.py
│   ├── safety_agent.py
│   ├── dubbing_agent.py
│   └── educational_agent.py
├── services/
│   ├── mux_service.py           # ffmpeg mux, not an agent
│   ├── gemini_client.py         # wrapper for calling Gemini, handles retry/rate-limit
│   ├── gcs_client.py
│   └── redis_client.py          # caches the glossary, caches dub results by hash
└── models/
    └── pipeline_context.py      # Pydantic model passed between agents
```

**Shared interface for every agent** (so agents can be added/removed without
touching `pipeline.py`):

```python
class PipelineContext(BaseModel):
    job_id: str
    project_id: str
    target_lang: str
    dialogue_lines: list[DialogueLine]
    characters: list[Character]
    current_stage: str

class Agent(Protocol):
    async def run(self, context: PipelineContext) -> PipelineContext: ...
```

**`pipeline.py` — checkpoint/resume logic:**

```python
AGENTS = [
    ("ingestion", IngestionAgent()),
    ("voice_profiling", VoiceProfilingAgent()),
    ("translation", TranslationAgent()),
    ("safety_check", SafetyAgent()),
    ("dubbing", DubbingAgent()),
]

async def run_pipeline(context: PipelineContext):
    start_index = get_stage_index(context.current_stage)  # resume if the worker previously crashed
    for stage_name, agent in AGENTS[start_index:]:
        context = await agent.run(context)
        context.current_stage = stage_name
        await save_checkpoint(context)          # write to Postgres
        await update_realtime_status(context)   # write to Firestore for the UI
    await educational_agent.run(context)        # runs in parallel, not on the critical path
```

## 3. Handling the Pub/Sub push in FastAPI (worker)

```python
@app.post("/pubsub/push")
async def handle_pubsub_push(request: PubSubPushRequest):
    verify_oidc_token(request)  # verify the message is genuinely from Pub/Sub, to prevent spoofing
    job_id = decode_message(request.message.data)
    context = await load_context(job_id)
    await run_pipeline(context)
    return Response(status_code=204)
```

The real message handler (see `worker/main.py::handle_pubsub_push`) shares a single
topic/subscription across several kinds of asynchronous work, distinguished by the
`type` field in the payload: `type="job"` (the default if the field is missing —
backward compatible with older messages) runs the full pipeline for `job_id`;
`type="voice_sample"` only listens to a voice sample the user just uploaded for a
given `character_id` and picks a TTS voice (see `POST
/characters/{id}/classify-voice-sample` in section 2), without touching
`_load_context`/the pipeline; `type="job_export"` (Video Editor,
specs/002-video-editor) renders the exported version from `jobs.timeline_edits`
via `worker/main.py::_export_job` — a SEPARATE type is used instead of `"job"`
because `current_stage="exporting"` is not part of `worker/pipeline.py`'s
`_STAGE_ORDER`, and mixing it into the "job" branch would be misinterpreted and
would re-run the entire pipeline from scratch.

## 4. Calling Gemini — a sample wrapper with retry/cache

```python
class GeminiClient:
    async def translate_dialogue(self, text: str, target_lang: str, is_song: bool) -> str:
        cache_key = f"translate:{hash(text)}:{target_lang}:{is_song}"
        if cached := await redis.get(cache_key):
            return cached
        prompt = SONG_TRANSCREATION_PROMPT if is_song else DIALOGUE_TRANSLATION_PROMPT
        result = await self._call_with_retry(prompt.format(text=text, lang=target_lang))
        await redis.set(cache_key, result, ex=86400)
        return result
```

- Cached by content + target language to avoid calling Gemini again when the
  worker retries after a network error (saves cost + time)
- `_call_with_retry` uses exponential backoff, limited to 3 attempts

## 5. Security & authentication

- Frontend → API: Firebase ID token in the `Authorization: Bearer <token>` header,
  verified with the Firebase Admin SDK
- Pub/Sub → Worker: uses the OIDC token that Pub/Sub automatically attaches on
  push, verifying the audience matches the worker service's URL (to prevent the
  endpoint from being called by a spoofed external caller)
- Signed GCS upload URL: limited to a 15-minute lifetime, restricted to specific
  file types (`video/mp4`, `video/quicktime`)

## 6. Script import format (CSV, script-first jobs)

(specs/003-script-first-ingestion, US1) Consumed by `POST
/projects/{project_id}/script-jobs` (section 2 above) via the frontend's "New
from Script" flow (`frontend/src/pages/NewScriptProject.tsx`). Lets a job
start directly from a locked text script instead of requiring an
already-recorded video, so translation/dubbing can run in parallel with the
video editor's own work — matching `docs/manual-dubbing-process.docx`'s own
parallel-execution workflow rather than waiting for a finished video. A
sample file is provided at `docs/sample-script-import.csv`.

**Header row** (required, exactly these 3 columns — order doesn't matter,
matched case-insensitively):

```
segment_code,character_name,text
```

| Column | Meaning |
|---|---|
| `segment_code` | A unique identifier for the row within this script (e.g. `001`, `S01E01-003`). Carried through to `dialogue_lines.segment_code` (see `database-schema.md`) and used to name the file when the dubbed audio is later downloaded as a zip (`GET /jobs/{id}/dubbed-audio-bundle`), so an editor can match it back to the original script. |
| `character_name` | The speaking character's name. Every distinct name in the file must have a voice manually assigned (`PUT /characters/{id}/voice`, picked from `GET /characters/voice-options`) before the script can be submitted — see US2 in `specs/003-script-first-ingestion/spec.md`. |
| `text` | The line's original-language dialogue text. |

**Rules, enforced both by the frontend parser
(`NewScriptProject.tsx::parseCsvLine`/`parseScriptCsv`) and re-validated
server-side so a hand-built request can't bypass them (`ScriptLineImport`/
`ScriptJobCreate`, `backend/api/schemas/schemas.py`):**

- Encoding: UTF-8, **no byte-order mark (BOM)**. A BOM prepended to the first
  header cell — common when Excel on Windows saves "CSV UTF-8" — makes the
  header fail to match `segment_code` and must be stripped before upload.
- Quoting: standard CSV quoting. Wrap a value in `"..."` if it contains a
  comma, and escape a literal `"` inside a quoted value by doubling it
  (`""`). An unquoted value containing a comma is rejected with a clear error
  rather than silently truncated.
- Blank lines are ignored.
- Every data row's `segment_code`, `character_name`, and `text` must be
  non-blank after trimming whitespace.
- `segment_code` must be unique across the whole file.

**Example** (also at `docs/sample-script-import.csv`):

```csv
segment_code,character_name,text
001,Mimi,"Hey, are you ready for the next quest?"
002,Dodo,"Always! Let's go, let's go!"
003,Mimi,"She said the treasure is past the ""singing bridge""."
```
