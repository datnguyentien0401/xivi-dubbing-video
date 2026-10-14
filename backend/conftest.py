"""Fixture SHARED by every test that needs a real Postgres instance (api/routers/test_*.py,
api/core/test_*.py, worker/test_main.py).

Real bug encountered (discovered 2026-10-09 while investigating a stuck job): several
test files defined their own `session` fixture like

    async with async_session() as s:
        yield s
        await s.rollback()

— but production code called DIRECTLY in tests (e.g.
`projects_router.create_project`, or `worker.main._export_job`) itself calls
`await session.commit()` INSIDE. A `rollback()` AFTER a `commit()` undoes
nothing (the commit already ended that transaction, so the next statement runs
in a NEW transaction). Real-world consequence: 447 fake users + 395 fake
projects + 227 fake source_videos leaked straight into the REAL dev database
over several weeks of running tests — because the DATABASE_URL used during
tests (`postgresql+asyncpg://xivi:xivi@localhost:5433/xivi`,
see the docstring of each test file) pointed EXACTLY at the same Postgres that
`docker compose up` uses for the real UI (port 5433 matches docker-compose.yml),
not a separate test DB.

The CORRECT fix — the standard SQLAlchemy pattern for test suites that use a
real DB (see
https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites):
open one real OUTERMOST connection + transaction BEFORE the test runs; every
Session used in the test (both the test's own and any used by the production
code it calls) MUST join that EXACT transaction via `join_transaction_mode=
"create_savepoint"` — each `session.commit()` then only ends ONE SAVEPOINT
(a nested transaction), NOT the outermost transaction. Rolling back the
outermost transaction at the end of the fixture wipes out EVERYTHING that
happened during the test, no matter how many times the code committed
internally.
"""

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

HAS_DATABASE_URL = "DATABASE_URL" in os.environ


@pytest_asyncio.fixture
async def session():
    """For tests that call router functions DIRECTLY with `session` passed as a
    parameter (see api/routers/test_*.py, api/core/test_*.py) — fully isolates
    from the real DB even when the function under test calls `commit()` internally."""
    if not HAS_DATABASE_URL:
        pytest.skip(
            "requires a real Postgres instance (see DATABASE_URL in the test file's docstring)"
        )
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.begin()
        async_session = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with async_session() as s:
            yield s
        # Do NOT call connection.commit() — the rollback() here is what actually
        # wipes out every savepoint created during the test, no matter how many
        # times the test / code-under-test "committed" (= closed a savepoint)
        # internally.
        await connection.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def worker_session_isolation(monkeypatch):
    """Same as `session` above, PLUS monkeypatches both
    `api.db.session.AsyncSessionLocal` and `worker.main.AsyncSessionLocal` —
    for tests that call into `worker/main.py` (e.g. `_export_job`,
    `handle_pubsub_push`), where production code opens its OWN session via
    `AsyncSessionLocal()` instead of receiving a session as a parameter like the
    router API does.
    Both locations are patched because `worker/main.py` imports the name
    `AsyncSessionLocal` into its own namespace when the module is loaded (`from
    api.db.session import AsyncSessionLocal`) — patching only
    `api.db.session.AsyncSessionLocal` would have no effect on that
    already-imported name; but some tests in worker/test_main.py do a FRESH
    `from api.db.session import AsyncSessionLocal` right inside the test
    function body (re-reading the attribute at call time, not at module-import
    time), so `api.db.session` still needs to be patched the same way.

    Returns the FIXTURE's own session so the test can create its own sample
    data (owner/project/...) using the EXACT session already joined to the
    savepoint — not by creating one through a raw `AsyncSessionLocal()` call
    (which would create a different session that, while joining the same
    savepoint, wastes an unnecessary connection checkout when one is already
    available)."""
    if not HAS_DATABASE_URL:
        pytest.skip(
            "requires a real Postgres instance (see DATABASE_URL in the test file's docstring)"
        )
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        await connection.begin()
        test_sessionmaker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr("api.db.session.AsyncSessionLocal", test_sessionmaker)
        monkeypatch.setattr("worker.main.AsyncSessionLocal", test_sessionmaker)
        async with test_sessionmaker() as s:
            yield s
        await connection.rollback()
    await engine.dispose()
