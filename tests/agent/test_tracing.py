import logging

import pytest

from argus.agent import tracing
from argus.config import OtelConfig


def test_disabled_tracing_does_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tracing, "_start", lambda config: pytest.fail("must not start"))

    assert tracing.setup_tracing(OtelConfig(enabled=False)) is False


def test_enabled_tracing_starts_with_the_configured_endpoint(monkeypatch: pytest.MonkeyPatch):
    started: list[OtelConfig] = []
    monkeypatch.setattr(tracing, "_start", started.append)
    config = OtelConfig(enabled=True, endpoint="http://phoenix:6006/v1/traces", project_name="p")

    assert tracing.setup_tracing(config) is True
    assert started == [config]


def test_tracing_failure_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def broken(config: OtelConfig) -> None:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(tracing, "_start", broken)

    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.setup_tracing(OtelConfig(enabled=True)) is False
    assert "continuing without traces" in caplog.text
