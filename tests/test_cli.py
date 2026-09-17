from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.providers.breakglass_provider import PENDING, BreakGlassStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help(runner: CliRunner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "agent" in result.output
    assert "breakglass" in result.output


def test_serve_without_token_fails_cleanly(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    config_path = tmp_path / "argus.yaml"
    config_path.write_text("providers: {}\n")

    result = runner.invoke(main, ["serve", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "ARGUS_MCP_TOKEN is not set" in result.output


def test_breakglass_list_without_config_or_store_fails(runner: CliRunner):
    result = runner.invoke(main, ["breakglass", "list"])

    assert result.exit_code != 0
    assert "Pass --config or --store" in result.output


def test_breakglass_list_is_empty_for_a_fresh_store(runner: CliRunner, tmp_path: Path):
    store_path = tmp_path / "breakglass.sqlite"
    BreakGlassStore(store_path)  # creates the schema

    result = runner.invoke(main, ["breakglass", "list", "--store", str(store_path)])

    assert result.exit_code == 0
    assert "No requests." in result.output


def test_breakglass_list_shows_a_pending_request(runner: CliRunner, tmp_path: Path):
    store_path = tmp_path / "breakglass.sqlite"
    store = BreakGlassStore(store_path)
    request = store.create(
        reason="pihole down", target_host="theseus", evidence="dig fails", proposed_objective="restart"
    )

    result = runner.invoke(main, ["breakglass", "list", "--store", str(store_path)])

    assert result.exit_code == 0
    assert request.id in result.output
    assert "theseus" in result.output
    assert "pihole down" in result.output


def test_breakglass_approve_updates_status(runner: CliRunner, tmp_path: Path):
    store_path = tmp_path / "breakglass.sqlite"
    store = BreakGlassStore(store_path)
    request = store.create(reason="r", target_host="theseus", evidence="e", proposed_objective="p")

    result = runner.invoke(main, ["breakglass", "approve", request.id, "--store", str(store_path)])

    assert result.exit_code == 0
    assert "Approved" in result.output
    assert store.get(request.id).status != PENDING


def test_breakglass_deny_updates_status(runner: CliRunner, tmp_path: Path):
    store_path = tmp_path / "breakglass.sqlite"
    store = BreakGlassStore(store_path)
    request = store.create(reason="r", target_host="theseus", evidence="e", proposed_objective="p")

    result = runner.invoke(main, ["breakglass", "deny", request.id, "--store", str(store_path)])

    assert result.exit_code == 0
    assert "Denied" in result.output
    assert store.get(request.id).status != PENDING


def test_breakglass_store_from_config(runner: CliRunner, tmp_path: Path):
    store_path = tmp_path / "breakglass.sqlite"
    store = BreakGlassStore(store_path)
    request = store.create(reason="r", target_host="theseus", evidence="e", proposed_objective="p")
    config_path = tmp_path / "argus.yaml"
    config_path.write_text(
        f"providers:\n  breakglass:\n    enabled: true\n    store_path: {store_path}\n"
    )

    result = runner.invoke(main, ["breakglass", "list", "--config", str(config_path)])

    assert result.exit_code == 0
    assert request.id in result.output
