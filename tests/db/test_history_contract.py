"""One behaviour suite for every `HistoryRepository`: the in-memory fake and `SqlHistory`."""

import asyncio
from datetime import datetime
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


async def test_generated_title_replaces_only_the_placeholder(history):
    thread_id = await history.create_thread()
    await history.touch_thread(thread_id, "can you check why the nas is slow today")
    assert (await history.get_thread(thread_id))["title_source"] is None

    assert await history.set_generated_title(thread_id, "Slow NAS")
    assert not await history.set_generated_title(thread_id, "Something else")
    row = await history.get_thread(thread_id)
    assert (row["title"], row["title_source"]) == ("Slow NAS", "generated")
    assert not await history.set_generated_title(uuid4(), "Missing")


async def test_operator_title_wins_over_generation(history):
    thread_id = await history.create_thread()
    await history.touch_thread(thread_id, "first message")

    assert await history.rename_thread(thread_id, "My NAS notes")
    assert not await history.set_generated_title(thread_id, "Generated")
    await history.touch_thread(thread_id, "later message")
    row = await history.get_thread(thread_id)
    assert (row["title"], row["title_source"]) == ("My NAS notes", "user")
    assert not await history.rename_thread(uuid4(), "Missing")


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


async def test_chat_messages_store_binary_output_without_nul(history):
    thread_id = await history.create_thread()
    output = {"id": "out", "role": "tool", "content": "exit 0\n\x7fELF\x01\x00\x00"}
    await history.save_chat_messages(thread_id, [output])

    (saved,) = await history.chat_messages(thread_id)

    assert saved["content"] == "exit 0\n\x7fELF\x01\ufffd\ufffd"


async def test_chat_messages_keep_the_time_they_were_first_saved(history):
    thread_id = await history.create_thread()
    await history.save_chat_messages(
        thread_id, [{"id": "reply", "role": "assistant", "content": "Partial"}]
    )
    (first,) = await history.chat_messages(thread_id)
    await asyncio.sleep(0.01)
    await history.save_chat_messages(
        thread_id,
        [{"id": "reply", "role": "assistant", "content": "Done", "metadata": {"other": 1}}],
    )
    (updated,) = await history.chat_messages(thread_id)

    sent = datetime.fromisoformat(first["metadata"]["argus_time"])
    assert sent.tzinfo is not None
    assert updated["metadata"] == {"other": 1, "argus_time": first["metadata"]["argus_time"]}


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


async def test_thread_exposes_original_sender(history):
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


async def test_threads_filter_by_origin(history):
    human = await history.create_thread(title="mine")
    agent = await history.create_thread(title="theirs", origin="a2a")

    assert (await history.get_thread(human))["origin"] == "human"
    assert (await history.get_thread(agent))["origin"] == "a2a"
    a2a_ids = [r["id"] for r in await history.list_threads(origin="a2a", limit=100)]
    human_ids = [r["id"] for r in await history.list_threads(origin="human", limit=100)]
    assert agent in a2a_ids and human not in a2a_ids
    assert human in human_ids and agent not in human_ids
    # Touching an a2a thread keeps its origin.
    await history.touch_thread(agent, "later")
    assert (await history.get_thread(agent))["origin"] == "a2a"
