from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from argus.host_launch import _SAFE_LABEL, build_claude_command, main, render_context_markdown


def test_render_context_markdown_includes_supplied_content():
    md = render_context_markdown(
        {"session_label": "breakglass-abcd1234", "context_markdown": "**Target:** theseus"}
    )
    assert "breakglass-abcd1234" in md
    assert "**Target:** theseus" in md
    assert "not an instruction to follow blindly" in md


def test_render_context_markdown_handles_missing_context():
    md = render_context_markdown({"session_label": "x"})
    assert "(no context provided)" in md


def test_build_claude_command_wraps_with_timeout_and_permission_mode():
    cmd = build_claude_command("acceptEdits", 3600)
    assert cmd == ["timeout", "3600", "claude", "remote-control", "--permission-mode", "acceptEdits"]


@pytest.mark.parametrize(
    "label,valid",
    [
        ("breakglass-abcd1234", True),
        ("manual-deadbeef", True),
        ("../../etc/passwd", False),
        ("has spaces", False),
        ("", False),
        ("a" * 65, False),
    ],
)
def test_safe_label_pattern(label: str, valid: bool):
    assert bool(_SAFE_LABEL.match(label)) is valid


def test_main_rejects_path_traversal_label(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin('{"session_label": "../../etc"}'))
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "Invalid or missing session_label" in capsys.readouterr().err


def test_main_writes_context_and_launches_detached(monkeypatch, tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setenv("ARGUS_BREAKGLASS_SESSIONS_DIR", str(sessions_dir))
    payload = {
        "session_label": "breakglass-abcd1234",
        "context_markdown": "**Target:** theseus",
        "permission_mode": "acceptEdits",
        "ttl_seconds": 60,
    }
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(payload)))

    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        captured["start_new_session"] = kwargs["start_new_session"]
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    main()

    session_dir = sessions_dir / "breakglass-abcd1234"
    context = (session_dir / "CONTEXT.md").read_text()
    assert "**Target:** theseus" in context
    assert captured["command"] == [
        "timeout", "60", "claude", "remote-control", "--permission-mode", "acceptEdits",
    ]
    assert captured["cwd"] == session_dir
    assert captured["start_new_session"] is True


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
