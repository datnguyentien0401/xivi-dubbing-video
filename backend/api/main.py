"""FastAPI orchestrator service — Cloud Run service #1.

Only receives HTTP requests from the frontend, does CRUD on the DB, generates
signed URLs, publishes to Pub/Sub. Does NOT call Gemini/ffmpeg directly here
(see AGENTS.md principle #4).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.core.config import settings
from api.routers import characters, jobs, projects

app = FastAPI(title="XIVI API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to real domains when deploying to production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(jobs.dialogue_lines_router)
app.include_router(characters.router)

if settings.use_local_storage:
    # Route faking GCS signed URLs for local dev — doesn't exist on the route
    # table when running against real GCS (see api/routers/local_storage.py).
    from api.routers import local_storage

    app.include_router(local_storage.router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
