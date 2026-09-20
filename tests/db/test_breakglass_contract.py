"""One behaviour suite for every `BreakGlassRepository`: the in-memory fake and `SqlBreakGlass`."""

import pytest

from argus.db.breakglass import APPROVED, DENIED, PENDING


async def _file(breakglass, reason="r", target_host="h"):
    return await breakglass.create_request(
        reason=reason, target_host=target_host, evidence="e", proposed_objective="p"
    )


async def test_create_defaults_to_pending_and_is_readable(breakglass):
    request = await _file(breakglass, reason="pihole down", target_host="theseus")

    assert request.status == PENDING
    assert request.id
    assert await breakglass.get_request(request.id) == request
    assert await breakglass.get_request("doesnotexist") is None


async def test_list_filters_by_status(breakglass):
    a = await _file(breakglass, "a")
    b = await _file(breakglass, "b")
    await breakglass.set_request_status(a.id, APPROVED)

    pending = {r.id for r in await breakglass.list_requests(status=PENDING)}
    approved = {r.id for r in await breakglass.list_requests(status=APPROVED)}

    assert b.id in pending and a.id not in pending
    assert a.id in approved and b.id not in approved


async def test_list_is_newest_first(breakglass):
    first = await _file(breakglass, "first")
    second = await _file(breakglass, "second")

    ids = [r.id for r in await breakglass.list_requests()]

    assert ids.index(second.id) < ids.index(first.id)


async def test_deciding_a_request_records_the_decision(breakglass):
    request = await _file(breakglass)

    updated = await breakglass.set_request_status(request.id, DENIED)

    assert updated.status == DENIED
    assert (await breakglass.get_request(request.id)).status == DENIED
    assert request.id not in {r.id for r in await breakglass.list_requests(status=PENDING)}


async def test_set_status_unknown_id_raises(breakglass):
    with pytest.raises(KeyError):
        await breakglass.set_request_status("doesnotexist", DENIED)
