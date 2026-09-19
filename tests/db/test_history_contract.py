"""One behaviour suite for every `HistoryRepository`: the in-memory fake and `SqlHistory`."""

from uuid import uuid4


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
