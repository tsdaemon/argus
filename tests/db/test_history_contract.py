"""One behaviour suite for every `HistoryRepository`: the in-memory fake and `SqlHistory`."""

from uuid import uuid4

import pytest


async def test_created_thread_is_readable(history):
    thread_id = await history.create_thread(title="diagnose theseus")

    row = await history.get_thread(thread_id)

    assert row["id"] == thread_id
    assert row["title"] == "diagnose theseus"
    assert row["created_at"] <= row["updated_at"]
    assert await history.get_thread(uuid4()) is None


async def test_threads_list_by_recent_activity_and_page(history):
    first, second, third = [await history.create_thread(title=t) for t in "abc"]
    await history.touch_thread(first, None)

    ids = [row["id"] for row in await history.list_threads(limit=500)]

    mine = [i for i in ids if i in {first, second, third}]
    assert mine == [first, third, second]
    page = await history.list_threads(limit=1, offset=ids.index(first))
    assert [row["id"] for row in page] == [first]


async def test_touch_keeps_the_first_title_and_creates_unknown_threads(history):
    thread_id = await history.create_thread()
    await history.touch_thread(thread_id, "First question")
    await history.touch_thread(thread_id, "Second question")
    assert (await history.get_thread(thread_id))["title"] == "First question"

    external = uuid4()  # started by an AG-UI client that never called create_thread
    await history.touch_thread(external, "From elsewhere")
    assert (await history.get_thread(external))["title"] == "From elsewhere"


async def test_chat_messages_upsert_in_first_seen_order(history):
    thread_id = await history.create_thread()
    await history.save_chat_messages(
        thread_id,
        [
            {"id": "first", "role": "user", "content": "Earlier question"},
            {"id": "second", "role": "assistant", "content": "Partial"},
        ],
    )
    await history.save_chat_messages(
        thread_id,
        [
            {"id": "second", "role": "assistant", "content": "Finished answer"},
            {"id": "third", "role": "user", "content": "Follow-up"},
        ],
    )

    saved = await history.chat_messages(thread_id)

    assert [m["id"] for m in saved] == ["first", "second", "third"]
    assert saved[1]["content"] == "Finished answer"
    assert await history.chat_messages(await history.create_thread()) == []


async def test_delete_removes_only_that_thread(history):
    doomed, kept = await history.create_thread(), await history.create_thread()
    for thread_id in (doomed, kept):
        await history.save_chat_messages(thread_id, [{"id": "m", "role": "user", "content": "hi"}])

    assert await history.delete_thread(doomed) is True

    assert await history.get_thread(doomed) is None
    assert await history.chat_messages(doomed) == []
    assert await history.get_thread(kept) is not None
    assert len(await history.chat_messages(kept)) == 1
    assert doomed not in [row["id"] for row in await history.list_threads(limit=500)]
    assert await history.delete_thread(doomed) is False


async def test_filter_threads_by_original_sender_before_pagination(history):
    human = await history.create_thread(title="Operator conversation")
    external = await history.create_thread(title="External conversation")
    author = {
        "kind": "agent",
        "name": "maintenance-bot",
        "token_id": "identity",
        "interface": "a2a",
    }
    await history.save_chat_messages(
        external,
        [
            {
                "id": "first-external",
                "role": "user",
                "content": "Inspect",
                "metadata": {"argus_author": author},
            },
            {"id": "operator-reply", "role": "user", "content": "Continue"},
        ],
    )
    assert (await history.get_thread(external))["author"] == author
    assert (await history.get_thread(human))["source"] == "human"
    agent_rows = await history.list_threads(source="agent", limit=100)
    assert any(row["id"] == external for row in agent_rows)
    assert not any(row["id"] == human for row in agent_rows)
    human_rows = await history.list_threads(source="human", limit=100)
    assert any(row["id"] == human for row in human_rows)
    assert not any(row["id"] == external for row in human_rows)
    first_page = await history.list_threads(source="agent", limit=1)
    assert len(first_page) == 1 and first_page[0]["source"] == "agent"


async def test_costs_add_up_per_thread_and_overall(history):
    before = await history.total_cost()
    thread_id = await history.create_thread()
    assert (await history.get_thread(thread_id))["cost_usd"] == 0
    updated_at = (await history.get_thread(thread_id))["updated_at"]

    await history.add_cost(thread_id, 0.0123)
    await history.add_cost(thread_id, 0.0002)
    await history.add_cost(uuid4(), 5.0)  # unknown thread: ignored

    row = await history.get_thread(thread_id)
    assert row["cost_usd"] == pytest.approx(0.0125)
    assert row["updated_at"] == updated_at
    assert [t["cost_usd"] for t in await history.list_threads(limit=500) if t["id"] == thread_id] \
        == [pytest.approx(0.0125)]
    assert await history.total_cost() - before == pytest.approx(0.0125)
