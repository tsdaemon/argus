from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from argus.launcher import build_launcher
from argus.launcher.ssh import SshHostLauncher


def make_fake_process(returncode: int, stderr: bytes = b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.asyncio
async def test_launch_sends_json_payload_over_stdin():
    launcher = SshHostLauncher(
        host="theseus.internal", user="argus-launcher", identity_file="/keys/id_ed25519",
        permission_mode="acceptEdits", ttl_seconds=3600,
    )
    fake_proc = make_fake_process(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await launcher.launch(session_label="breakglass-abcd1234", context_markdown="do the thing")

    args, _kwargs = mock_exec.call_args
    assert args == (
        "ssh", "-o", "BatchMode=yes", "-p", "22",
        "-i", "/keys/id_ed25519", "argus-launcher@theseus.internal",
    )
    sent_payload = json.loads(fake_proc.communicate.call_args.args[0])
    assert sent_payload == {
        "session_label": "breakglass-abcd1234",
        "context_markdown": "do the thing",
        "permission_mode": "acceptEdits",
        "ttl_seconds": 3600,
    }


@pytest.mark.asyncio
async def test_launch_raises_on_nonzero_exit():
    launcher = SshHostLauncher(host="h", user="u")
    fake_proc = make_fake_process(returncode=255, stderr=b"Host key verification failed.")

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)),
        pytest.raises(RuntimeError, match="Host key verification failed"),
    ):
        await launcher.launch(session_label="x", context_markdown="y")


@pytest.mark.asyncio
async def test_ttl_omitted_from_payload_when_not_configured():
    launcher = SshHostLauncher(host="h", user="u")
    fake_proc = make_fake_process(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        await launcher.launch(session_label="x", context_markdown="y")

    sent_payload = json.loads(fake_proc.communicate.call_args.args[0])
    assert "ttl_seconds" not in sent_payload


def test_build_launcher_returns_none_without_config():
    assert build_launcher(None) is None
    assert build_launcher({}) is None


def test_build_launcher_constructs_ssh_launcher():
    launcher = build_launcher({"type": "ssh", "host": "h", "user": "u"})
    assert isinstance(launcher, SshHostLauncher)


def test_build_launcher_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unknown launcher type"):
        build_launcher({"type": "carrier-pigeon"})
