import os

import pytest

TEST_DATABASE_URL = os.environ.get(
    "ARGUS_TEST_DATABASE_URL", "postgresql://argus:testpass123@localhost:5432/argus"
)


@pytest.fixture
async def db_pool():
    """A real Postgres connection pool for `tests/db/test_repo.py`. Skips (doesn't
    fail) if nothing is listening — bring one up with `docker compose up -d postgres`,
    migrated via `alembic upgrade head`."""
    from psycopg_pool import AsyncConnectionPool

    try:
        pool = AsyncConnectionPool(TEST_DATABASE_URL, open=False)
        await pool.open(wait=True, timeout=2)
    except Exception as exc:  # noqa: BLE001 — any connection failure should skip, not fail
        pytest.skip(f"no reachable Postgres at {TEST_DATABASE_URL} ({exc})")
    yield pool
    await pool.close()
