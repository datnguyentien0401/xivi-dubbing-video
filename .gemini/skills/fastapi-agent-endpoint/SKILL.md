---
name: fastapi-agent-endpoint
description: Add a new endpoint to backend/api (the FastAPI orchestrator service) following XIVI project conventions — router, Pydantic schema, Firebase auth, no calling Gemini directly from the API service.
---

# Skill: Add a new FastAPI Endpoint

## Required principles
- Endpoints in `backend/api/` **never** call Gemini or process audio/video
  directly — that belongs to `backend/worker/`. The API service only does: DB CRUD,
  generating signed URLs, publishing Pub/Sub messages, and returning a fast response.
- Every request/response body must have a corresponding Pydantic schema in
  `backend/api/schemas/schemas.py` — never accept/return a raw `dict`.
- Every endpoint that requires a logged-in user must use the
  `backend/api/core/firebase_auth.py::get_current_user` dependency.

## Steps
1. Identify the appropriate router in `backend/api/routers/` (projects / jobs /
   characters / dialogue_lines), or create a new router if it's a different domain.
2. Add the Pydantic schema (request + response) to `schemas/schemas.py`.
3. Write the endpoint function, using `Depends(get_current_user)` if auth is needed.
4. If the endpoint creates a long-running job → publish a message via
   `backend/api/core/pubsub_client.py`, do NOT run the processing immediately in the request.
5. Register the router in `backend/api/main.py` if it's a new router.
6. Add a basic test (status code, response schema) using FastAPI's `TestClient`.

## Checklist
- [ ] There's a Pydantic schema for both request and response
- [ ] The endpoint doesn't block for more than ~200ms (no direct Gemini/ffmpeg calls)
- [ ] The auth dependency is applied where needed
- [ ] The router is registered in `main.py`
