from __future__ import annotations

from pathlib import Path

import pytest

from argus.providers.breakglass_provider import APPROVED, DENIED, PENDING, BreakGlassStore


@pytest.fixture
def store(tmp_path: Path) -> BreakGlassStore:
    return BreakGlassStore(tmp_path / "breakglass.sqlite")


def test_create_defaults_to_pending(store: BreakGlassStore):
    r = store.create(
        reason="pihole down", target_host="theseus", evidence="ping fails",
        proposed_objective="restart pihole via ssh",
    )
    assert r.status == PENDING
    assert r.id
    assert store.get(r.id) == r


def test_list_filters_by_status(store: BreakGlassStore):
    a = store.create(reason="a", target_host="h1", evidence="e", proposed_objective="p")
    b = store.create(reason="b", target_host="h2", evidence="e", proposed_objective="p")
    store.set_status(a.id, APPROVED)

    pending = store.list(status=PENDING)
    approved = store.list(status=APPROVED)

    assert [r.id for r in pending] == [b.id]
    assert [r.id for r in approved] == [a.id]


def test_denying_a_request_records_the_decision(store: BreakGlassStore):
    request = store.create(reason="r", target_host="h", evidence="e", proposed_objective="p")

    store.set_status(request.id, DENIED)

    assert store.get(request.id).status == DENIED
    assert store.list(status=PENDING) == []


def test_set_status_unknown_id_raises(store: BreakGlassStore):
    with pytest.raises(KeyError):
        store.set_status("doesnotexist", DENIED)


def test_store_persists_across_instances(tmp_path: Path):
    path = tmp_path / "breakglass.sqlite"
    r = BreakGlassStore(path).create(
        reason="a", target_host="h1", evidence="e", proposed_objective="p"
    )
    reopened = BreakGlassStore(path)
    assert reopened.get(r.id) == r
