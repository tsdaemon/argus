from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from argus.providers.breakglass_provider import APPROVED, PENDING, BreakGlassStore
from argus.webapp import SESSION_COOKIE, create_app
from argus.webauth import AdminUserStore


@pytest.fixture
def store(tmp_path: Path) -> BreakGlassStore:
    return BreakGlassStore(tmp_path / "breakglass.sqlite")


@pytest.fixture
def admin_store(tmp_path: Path) -> AdminUserStore:
    return AdminUserStore(tmp_path / "breakglass.sqlite")


@pytest.fixture
def client(store: BreakGlassStore, admin_store: AdminUserStore) -> TestClient:
    return TestClient(create_app(store, admin_store))


def _provision_and_login(client: TestClient, admin_store: AdminUserStore) -> None:
    """Provisions the (one-time) admin account directly and logs the client in —
    the plaintext password is only ever returned once, by get_or_create() itself."""
    account, password = admin_store.get_or_create()
    assert password is not None, "expected this call to be the one that creates the account"
    resp = client.post("/login", data={"username": account.username, "password": password})
    assert resp.status_code == 200


def test_first_visit_to_login_creates_account_and_reveals_password(
    client: TestClient, admin_store: AdminUserStore
):
    assert admin_store.get() is None

    resp = client.get("/login")

    assert resp.status_code == 200
    account = admin_store.get()
    assert account is not None
    assert "Admin account created" in resp.text
    assert "<script" not in resp.text


def test_second_visit_to_login_does_not_regenerate_or_reveal(
    client: TestClient, admin_store: AdminUserStore
):
    client.get("/login")
    first_account = admin_store.get()

    resp = client.get("/login")

    assert "Admin account created" not in resp.text
    assert admin_store.get() == first_account


def test_breakglass_without_session_redirects_to_login(client: TestClient):
    resp = client.get("/breakglass")
    assert resp.status_code == 200  # TestClient follows the redirect
    assert resp.url.path == "/login"


def test_wrong_password_does_not_authenticate(client: TestClient, admin_store: AdminUserStore):
    account, _password = admin_store.get_or_create()

    resp = client.post("/login", data={"username": account.username, "password": "not-it"})

    assert resp.url.path == "/login"
    assert "error" in str(resp.url.query)
    assert SESSION_COOKIE not in client.cookies


def test_correct_password_authenticates_and_lists_pending(
    client: TestClient, store: BreakGlassStore, admin_store: AdminUserStore
):
    store.create(reason="pihole down", target_host="theseus", evidence="dig fails", proposed_objective="restart")
    _provision_and_login(client, admin_store)

    resp = client.get("/breakglass")

    assert resp.status_code == 200
    assert "theseus" in resp.text
    assert "pihole down" in resp.text
    assert "<script" not in resp.text


def test_logout_clears_session(client: TestClient, admin_store: AdminUserStore):
    _provision_and_login(client, admin_store)
    assert client.get("/breakglass").status_code == 200

    client.post("/logout")

    assert client.get("/breakglass").url.path == "/login"


def test_approve_flips_status_and_removes_from_pending_list(
    client: TestClient, store: BreakGlassStore, admin_store: AdminUserStore
):
    request = store.create(reason="r", target_host="h", evidence="e", proposed_objective="p")
    _provision_and_login(client, admin_store)

    resp = client.post(f"/breakglass/{request.id}/approve")

    assert resp.status_code == 200
    assert store.get(request.id).status == APPROVED
    assert store.list(status=PENDING) == []


def test_decide_without_session_redirects_to_login(client: TestClient, store: BreakGlassStore):
    request = store.create(reason="r", target_host="h", evidence="e", proposed_objective="p")

    resp = client.post(f"/breakglass/{request.id}/approve")

    assert resp.url.path == "/login"
    assert store.get(request.id).status == PENDING


def test_decide_on_unknown_id_returns_404(client: TestClient, admin_store: AdminUserStore):
    _provision_and_login(client, admin_store)
    resp = client.post("/breakglass/doesnotexist/approve")
    assert resp.status_code == 404


def test_deny_does_not_trigger_launcher(store: BreakGlassStore, admin_store: AdminUserStore):
    launcher = FakeLauncher()
    client = TestClient(create_app(store, admin_store, launcher))
    _provision_and_login(client, admin_store)
    request = store.create(reason="r", target_host="h", evidence="e", proposed_objective="p")

    client.post(f"/breakglass/{request.id}/deny")

    assert launcher.calls == []


class FakeLauncher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    async def launch(self, *, session_label: str, context_markdown: str) -> None:
        self.calls.append({"session_label": session_label, "context_markdown": context_markdown})
        if self._fail:
            raise RuntimeError("ssh exploded")


def test_approve_triggers_launcher_with_request_context(store: BreakGlassStore, admin_store: AdminUserStore):
    launcher = FakeLauncher()
    client = TestClient(create_app(store, admin_store, launcher))
    _provision_and_login(client, admin_store)
    request = store.create(
        reason="pihole down", target_host="theseus", evidence="dig fails", proposed_objective="restart"
    )

    resp = client.post(f"/breakglass/{request.id}/approve")

    assert resp.status_code == 200
    assert len(launcher.calls) == 1
    call = launcher.calls[0]
    assert call["session_label"] == f"breakglass-{request.id}"
    assert "pihole down" in call["context_markdown"]
    assert "theseus" in call["context_markdown"]


def test_approve_surfaces_launcher_failure_instead_of_hiding_it(
    store: BreakGlassStore, admin_store: AdminUserStore
):
    launcher = FakeLauncher(fail=True)
    client = TestClient(create_app(store, admin_store, launcher))
    _provision_and_login(client, admin_store)
    request = store.create(reason="r", target_host="h", evidence="e", proposed_objective="p")

    resp = client.post(f"/breakglass/{request.id}/approve")

    assert resp.status_code == 502
    assert "ssh exploded" in resp.text
    # The approval itself still went through — only the launch failed.
    assert store.get(request.id).status == APPROVED


def test_launch_page_404s_without_a_configured_launcher(client: TestClient, admin_store: AdminUserStore):
    _provision_and_login(client, admin_store)
    resp = client.get("/launch")
    assert resp.status_code == 404


def test_launch_page_requires_session(store: BreakGlassStore, admin_store: AdminUserStore):
    client = TestClient(create_app(store, admin_store, FakeLauncher()))
    resp = client.get("/launch")
    assert resp.url.path == "/login"


def test_manual_launch_submits_free_text_context(store: BreakGlassStore, admin_store: AdminUserStore):
    launcher = FakeLauncher()
    client = TestClient(create_app(store, admin_store, launcher))
    _provision_and_login(client, admin_store)

    resp = client.post("/launch", data={"context": "investigate disk usage on theseus"})

    assert resp.status_code == 200
    assert len(launcher.calls) == 1
    assert launcher.calls[0]["context_markdown"] == "investigate disk usage on theseus"
    assert launcher.calls[0]["session_label"].startswith("manual-")
    # Not tied to any break-glass request:
    assert store.list() == []


def test_manual_launch_rejects_empty_context(store: BreakGlassStore, admin_store: AdminUserStore):
    launcher = FakeLauncher()
    client = TestClient(create_app(store, admin_store, launcher))
    _provision_and_login(client, admin_store)

    resp = client.post("/launch", data={"context": "   "})

    assert resp.status_code == 400
    assert launcher.calls == []


def test_pending_list_shows_launch_link_only_when_launcher_configured(
    store: BreakGlassStore, admin_store: AdminUserStore, tmp_path: Path
):
    without_launcher = TestClient(create_app(store, admin_store, None))
    _provision_and_login(without_launcher, admin_store)

    other_path = tmp_path / "other.sqlite"
    with_launcher_store = BreakGlassStore(other_path)
    with_launcher_admin = AdminUserStore(other_path)
    with_launcher = TestClient(create_app(with_launcher_store, with_launcher_admin, FakeLauncher()))
    _provision_and_login(with_launcher, with_launcher_admin)

    assert "/launch" not in without_launcher.get("/breakglass").text
    assert "/launch" in with_launcher.get("/breakglass").text
