"""Backfill thread costs from Phoenix, for spend recorded before argus counted it itself.

Each argus thread is a Phoenix session (`session.id` is the thread id), and Phoenix sums the
cost of its spans per session. A thread whose Phoenix total exceeds its recorded cost is
raised to it, so a re-run changes nothing. Phoenix prunes traces after its retention period,
so older spend may be gone.

    ARGUS_CONFIG=examples/argus.dev.yaml python -m argus.agent.phoenix_costs

Phoenix is reached at the origin of `agent.otel.endpoint` (OTLP over HTTP shares its port),
with `PHOENIX_API_KEY` as a bearer token when set.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlsplit

import httpx

from argus.config import load_config
from argus.db.history import HistoryRepository, SqlHistory, make_engine

_BATCH = 50


def phoenix_url(otel_endpoint: str) -> str:
    parts = urlsplit(otel_endpoint)
    if not parts.path.startswith("/v1/traces"):
        raise ValueError(
            f"{otel_endpoint} is not an OTLP/HTTP endpoint; Phoenix's API is on its HTTP port."
        )
    return f"{parts.scheme}://{parts.netloc}"


async def session_costs(client: httpx.AsyncClient, session_ids: list[str]) -> dict[str, float]:
    """Phoenix's total cost per session id; a session Phoenix does not have is left out."""
    fields = " ".join(
        f's{i}: getProjectSessionById(sessionId: "{sid}") {{ costSummary {{ total {{ cost }} }} }}'
        for i, sid in enumerate(session_ids)
    )
    response = await client.post("/graphql", json={"query": f"{{ {fields} }}"})
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Phoenix GraphQL error: {body['errors']}")
    costs = {}
    for i, sid in enumerate(session_ids):
        session = body["data"][f"s{i}"]
        cost = session and session["costSummary"]["total"]["cost"]
        if cost:
            costs[sid] = float(cost)
    return costs


async def backfill(history: HistoryRepository, client: httpx.AsyncClient) -> list[tuple]:
    """Raise each thread's cost to its Phoenix total; returns (thread, before, after) changes."""
    changes = []
    offset = 0
    while threads := await history.list_threads(limit=_BATCH, offset=offset):
        offset += len(threads)
        phoenix = await session_costs(client, [str(t["id"]) for t in threads])
        for thread in threads:
            missing = phoenix.get(str(thread["id"]), 0.0) - thread["cost_usd"]
            if missing > 1e-9:
                await history.add_cost(thread["id"], missing)
                changes.append((thread, thread["cost_usd"], thread["cost_usd"] + missing))
    return changes


async def main() -> None:
    config = load_config(os.environ["ARGUS_CONFIG"])
    key = os.environ.get("PHOENIX_API_KEY")
    engine = make_engine(config.agent.database_url)
    try:
        async with httpx.AsyncClient(
            base_url=phoenix_url(config.agent.otel.endpoint),
            headers={"Authorization": f"Bearer {key}"} if key else None,
            timeout=30,
        ) as client:
            changes = await backfill(SqlHistory(engine), client)
    finally:
        await engine.dispose()
    for thread, before, after in changes:
        print(f"{thread['id']}  ${before:.4f} -> ${after:.4f}  {thread['title'] or ''}"[:120])
    print(f"Updated {len(changes)} thread(s), +${sum(a - b for _, b, a in changes):.4f}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        sys.exit(f"Backfill failed: {exc!r}")
