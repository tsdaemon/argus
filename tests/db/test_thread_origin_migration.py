"""Migration a7d3c5e92f41 on real Postgres: backfill marks only A2A-owned threads."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.db.conftest import TEST_DATABASE_URL

PREVIOUS, REVISION = "d4c2e8a71b05", "a7d3c5e92f41"


@pytest.fixture
def blank_schema(monkeypatch):
    """An empty throwaway schema; unlike `postgres_url`, nothing is migrated yet."""
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no reachable Postgres at ARGUS_TEST_DATABASE_URL ({exc})")
    schema = "argus_test_" + uuid4().hex
    with conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
        monkeypatch.setenv("ARGUS_AGENT_DATABASE_URL", TEST_DATABASE_URL)
        try:
            yield schema, conn
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(conn, schema):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'threads'",
        (schema,),
    ).fetchall()
    return {r[0] for r in rows}


def test_backfill_marks_only_a2a_threads_and_downgrade_reverts(blank_schema):
    schema, conn = blank_schema
    command.upgrade(_config(), PREVIOUS)
    assert "origin" not in _columns(conn, schema)

    human, owned, empty, rotated, chatty = (uuid4() for _ in range(5))
    token = uuid4()
    for thread in (human, owned, empty, rotated, chatty):
        conn.execute(
            sql.SQL("INSERT INTO {}.threads (id) VALUES (%s)").format(sql.Identifier(schema)),
            (thread,),
        )
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.api_tokens (id, name, secret_hash, interface, created_at) "
            "VALUES (%s, 'agent', 'x', 'a2a', now())"
        ).format(sql.Identifier(schema)),
        (token,),
    )
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.a2a_contexts (id, token_id, thread_id) VALUES ('ctx', %s, %s)"
        ).format(sql.Identifier(schema)),
        (token, owned),
    )

    def say(thread, role, author):
        content = {"role": role, "metadata": {"argus_author": author} if author else {}}
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content) VALUES (%s, %s, %s, %s)",
            (uuid4(), thread, role, Jsonb(content)),
        )

    agent, person = {"kind": "agent", "name": "hermes"}, {"kind": "human", "name": "operator"}
    say(rotated, "user", agent)  # orphaned by idle rotation: no a2a_contexts row
    say(rotated, "assistant", None)
    say(chatty, "user", person)  # first user turn decides, not later ones
    say(chatty, "assistant", None)
    say(chatty, "user", agent)
    say(human, "user", None)  # no author metadata at all

    command.upgrade(_config(), REVISION)
    origins = dict(conn.execute("SELECT id, origin FROM threads").fetchall())
    assert origins == {
        human: "human",
        owned: "a2a",
        empty: "human",
        rotated: "a2a",
        chatty: "human",
    }

    # Threads created after the migration default to human.
    fresh = uuid4()
    conn.execute("INSERT INTO threads (id) VALUES (%s)", (fresh,))
    assert conn.execute("SELECT origin FROM threads WHERE id = %s", (fresh,)).fetchone() == (
        "human",
    )

    command.downgrade(_config(), PREVIOUS)
    assert "origin" not in _columns(conn, schema)
    assert conn.execute("SELECT count(*) FROM threads").fetchone() == (6,)
