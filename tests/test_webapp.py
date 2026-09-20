from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from argus.db.breakglass import APPROVED, PENDING
from argus.mcp.webapp import create_app
from argus.webauth import SESSION_COOKIE, AdminAuth, sign_session
from tests.fakes import InMemoryAdmin, InMemoryBreakGlass


def run(coro):
    """The repository is async and these tests are not; the fake is not tied to a loop."""
    return asyncio.run(coro)


def file_request(store, *, reason="r", target_host="h", evidence="e", objective="p"):
    return run(
        store.create_request(
            reason=reason,
            target_host=target_host,
            evidence=evidence,
            proposed_objective=objective,
        )
    )


@pytest.fixture
def store() -> InMemoryBreakGlass:
    return InMemoryBreakGlass()


@pytest.fixture
def admin() -> AdminAuth:
    return AdminAuth(InMemoryAdmin())


@pytest.fixture
def client(store: InMemoryBreakGlass, admin: AdminAuth) -> TestClient:
    return TestClient(create_app(store, admin))


def _provision_and_login(client: TestClient, admin: AdminAuth) -> None:
    """Creates the admin account directly and gives the client a valid session for it. The
    login form itself belongs to `argus.api.auth` and is tested in tests/api/test_auth.py."""
    account, _password = run(admin.get_or_create())
    client.cookies.set(SESSION_COOKIE, sign_session(account.session_secret, account.username))


def test_breakglass_without_session_redirects_to_login(client: TestClient):
    resp = client.get("/breakglass", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_pending_requests_are_listed_for_a_logged_in_admin(
    client: TestClient, store: InMemoryBreakGlass, admin: AdminAuth
):
    file_request(
        store, reason="pihole down", target_host="theseus", evidence="dig fails",
        objective="restart",
    )
    _provision_and_login(client, admin)

    resp = client.get("/breakglass")

    assert resp.status_code == 200
    assert "theseus" in resp.text
    assert "pihole down" in resp.text
    assert "<script" not in resp.text


def test_approve_flips_status_and_removes_from_pending_list(
    client: TestClient, store: InMemoryBreakGlass, admin: AdminAuth
):
    request = file_request(store)
    _provision_and_login(client, admin)

    resp = client.post(f"/breakglass/{request.id}/approve")

    assert resp.status_code == 200
    assert run(store.get_request(request.id)).status == APPROVED
    assert run(store.list_requests(status=PENDING)) == []


def test_decide_without_session_redirects_to_login(client: TestClient, store: InMemoryBreakGlass):
    request = file_request(store)

    resp = client.post(f"/breakglass/{request.id}/approve", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert run(store.get_request(request.id)).status == PENDING


def test_decide_on_unknown_id_returns_404(client: TestClient, admin: AdminAuth):
    _provision_and_login(client, admin)
    resp = client.post("/breakglass/doesnotexist/approve")
    assert resp.status_code == 404


class FakeLauncher:
    def __init__(self, *, hosts=("theseus", "eyes"), fail: bool = False) -> None:
        self.hosts = list(hosts)
        self.calls: list[dict] = []
        self._fail = fail

    async def launch(self, *, host: str, session_label: str, context_markdown: str) -> None:
        self.calls.append(
            {"host": host, "session_label": session_label, "context_markdown": context_markdown}
        )
        if self._fail:
            raise RuntimeError("ssh exploded")


def _client_with(store, admin, launcher) -> TestClient:
    client = TestClient(create_app(store, admin, launcher))
    _provision_and_login(client, admin)
    return client


def test_deny_does_not_trigger_launcher(store: InMemoryBreakGlass, admin: AdminAuth):
    launcher = FakeLauncher()
    client = _client_with(store, admin, launcher)
    request = file_request(store)

    client.post(f"/breakglass/{request.id}/deny")

    assert launcher.calls == []


def test_approve_triggers_launcher_on_the_chosen_host(store: InMemoryBreakGlass, admin: AdminAuth):
    launcher = FakeLauncher()
    client = _client_with(store, admin, launcher)
    request = file_request(
        store, reason="pihole down", target_host="theseus", evidence="dig fails",
        objective="restart",
    )

    resp = client.post(f"/breakglass/{request.id}/approve", data={"host": "eyes"})

    assert resp.status_code == 200
    assert len(launcher.calls) == 1
    call = launcher.calls[0]
    assert call["host"] == "eyes"
    assert call["session_label"] == f"breakglass-{request.id}"
    assert "pihole down" in call["context_markdown"]
    assert "theseus" in call["context_markdown"]


def test_approve_needs_a_configured_host_and_changes_nothing_without_one(
    store: InMemoryBreakGlass, admin: AdminAuth
):
    launcher = FakeLauncher()
    client = _client_with(store, admin, launcher)
    request = file_request(store)

    for data in ({}, {"host": "not-configured"}):
        resp = client.post(f"/breakglass/{request.id}/approve", data=data)
        assert resp.status_code == 400

    assert launcher.calls == []
    assert run(store.get_request(request.id)).status == PENDING


def test_approve_surfaces_launcher_failure_instead_of_hiding_it(
    store: InMemoryBreakGlass, admin: AdminAuth
):
    launcher = FakeLauncher(fail=True)
    client = _client_with(store, admin, launcher)
    request = file_request(store)

    resp = client.post(f"/breakglass/{request.id}/approve", data={"host": "theseus"})

    assert resp.status_code == 502
    assert "ssh exploded" in resp.text
    # The approval itself still went through — only the launch failed.
    assert run(store.get_request(request.id)).status == APPROVED


def test_launch_page_404s_without_a_configured_launcher(client: TestClient, admin: AdminAuth):
    _provision_and_login(client, admin)
    resp = client.get("/launch")
    assert resp.status_code == 404


def test_launch_page_requires_session(store: InMemoryBreakGlass, admin: AdminAuth):
    client = TestClient(create_app(store, admin, FakeLauncher()))
    resp = client.get("/launch", follow_redirects=False)
    assert resp.headers["location"] == "/login"


def test_manual_launch_submits_free_text_context_to_the_chosen_host(
    store: InMemoryBreakGlass, admin: AdminAuth
):
    launcher = FakeLauncher()
    client = _client_with(store, admin, launcher)

    resp = client.post(
        "/launch", data={"host": "eyes", "context": "investigate disk usage on theseus"}
    )

    assert resp.status_code == 200
    assert len(launcher.calls) == 1
    assert launcher.calls[0]["host"] == "eyes"
    assert launcher.calls[0]["context_markdown"] == "investigate disk usage on theseus"
    assert launcher.calls[0]["session_label"].startswith("manual-")
    # Not tied to any break-glass request:
    assert run(store.list_requests()) == []


def test_manual_launch_rejects_empty_context_and_unknown_host(
    store: InMemoryBreakGlass, admin: AdminAuth
):
    launcher = FakeLauncher()
    client = _client_with(store, admin, launcher)

    assert client.post("/launch", data={"host": "eyes", "context": "   "}).status_code == 400
    assert client.post("/launch", data={"host": "nope", "context": "x"}).status_code == 400
    assert launcher.calls == []


def test_pages_offer_the_configured_hosts_and_preselect_the_target(
    store: InMemoryBreakGlass, admin: AdminAuth
):
    client = _client_with(store, admin, FakeLauncher())
    file_request(store, target_host="eyes")

    board = client.get("/breakglass").text
    form = client.get("/launch").text

    assert 'value="eyes" form="approve-' in board and " checked" in board.split('value="eyes"')[1]
    assert 'value="theseus" form="approve-' in board
    assert "checked" not in board.split('value="theseus"')[1].split("</label>")[0]
    assert 'value="theseus" checked' in form  # nothing to match, so the first host is picked
    assert 'name="host"' in form


def test_pending_list_shows_launch_link_only_when_launcher_configured():
    def logged_in(launcher):
        auth = AdminAuth(InMemoryAdmin())
        client = TestClient(create_app(InMemoryBreakGlass(), auth, launcher))
        _provision_and_login(client, auth)
        return client

    assert "/launch" not in logged_in(None).get("/breakglass").text
    assert "/launch" in logged_in(FakeLauncher()).get("/breakglass").text
