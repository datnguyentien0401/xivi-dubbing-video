"""Redis client — used for 3 specific purposes (see docs/tech-stack.md section 3):
1. Caching Gemini call results (avoids re-calling on retry)
2. Caching the glossary to keep proper nouns consistent across the project
3. Locking by job_id to prevent duplicate processing (see
   worker/main.py:handle_pubsub_push) — Pub/Sub redelivers a message when the
   previous response errored/timed out, so a single job_id could end up
   processed by multiple concurrent requests if not locked.
"""

import redis.asyncio as redis


class RedisClient:
    def __init__(self, url: str):
        self._client = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        await self._client.set(key, value, ex=ttl)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        """SET NX — true if the lock was acquired (no one holds it, or the
        old lock has expired), false if it's currently held by another
        request."""
        return bool(await self._client.set(key, "1", nx=True, ex=ttl_seconds))

    async def release_lock(self, key: str) -> None:
        await self._client.delete(key)
