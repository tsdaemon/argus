"""Optional real-Postgres fixtures. Every test using them skips when no database answers.

The schema is never hand-written here: a throwaway schema is created and `alembic upgrade
head` builds the tables, so these tests also prove the migrations produce what the models
expect. Nothing touches the developer's own tables.
"""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from argus.db.admin import SqlAdmin
from argus.db.breakglass import SqlBreakGlass
from argus.db.history import SqlHistory, make_engine
from tests.fakes import InMemoryAdmin, InMemoryBreakGlass, InMemoryHistory

TEST_DATABASE_URL = os.environ.get(
    "ARGUS_TEST_DATABASE_URL", "postgresql://argus:testpass123@localhost:5432/argus"
)


@pytest.fixture(scope="session")
def postgres_url():
    """A migrated, throwaway schema in the test database, selected through PGOPTIONS."""
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no reachable Postgres at ARGUS_TEST_DATABASE_URL ({exc})")
    schema = "argus_test_" + uuid4().hex
    patch = pytest.MonkeyPatch()
    with conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            patch.setenv("PGOPTIONS", f"-c search_path={schema}")
            patch.setenv("ARGUS_AGENT_DATABASE_URL", TEST_DATABASE_URL)
            root = Path(__file__).resolve().parents[2]
            command.upgrade(Config(str(root / "alembic.ini")), "head")
            yield TEST_DATABASE_URL
        finally:
            patch.undo()
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
async def sql_engine(postgres_url):
    engine = make_engine(postgres_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def sql_history(sql_engine):
    return SqlHistory(sql_engine)


@pytest.fixture(params=["memory", "sql"])
def history(request):
    """The same suite runs against the in-memory fake and, when reachable, Postgres."""
    if request.param == "memory":
        return InMemoryHistory()
    return request.getfixturevalue("sql_history")


@pytest.fixture
def sql_breakglass(sql_engine):
    return SqlBreakGlass(sql_engine)


@pytest.fixture(params=["memory", "sql"])
def breakglass(request):
    """The same suite runs against the in-memory fake and, when reachable, Postgres."""
    if request.param == "memory":
        return InMemoryBreakGlass()
    return request.getfixturevalue("sql_breakglass")


@pytest.fixture
def sql_admin(sql_engine):
    return SqlAdmin(sql_engine)


@pytest.fixture(params=["memory", "sql"])
def admin_repo(request):
    if request.param == "memory":
        return InMemoryAdmin()
    return request.getfixturevalue("sql_admin")
