from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from argus.launcher import build_launcher
from argus.launcher.ssh import PROTOCOL, SshHostLauncher, SshTarget


def make_fake_process(returncode: int, stderr: bytes = b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


def launcher_for(**hosts: SshTarget) -> SshHostLauncher:
    return SshHostLauncher(hosts)


@pytest.mark.asyncio
async def test_launch_sends_json_payload_over_stdin():
    launcher = launcher_for(
        theseus=SshTarget(
            host="theseus.internal", user="argus-launcher", identity_file="/keys/id_ed25519",
            permission_mode="acceptEdits", ttl_seconds=3600,
        )
    )
    fake_proc = make_fake_process(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await launcher.launch(
            host="theseus", session_label="breakglass-abcd1234", context_markdown="do the thing"
        )

    args, _kwargs = mock_exec.call_args
    assert args == (
        "ssh", "-o", "BatchMode=yes", "-p", "22",
        "-i", "/keys/id_ed25519", "argus-launcher@theseus.internal",
    )
    sent_payload = json.loads(fake_proc.communicate.call_args.args[0])
    assert sent_payload == {
        "protocol": PROTOCOL,
        "session_label": "breakglass-abcd1234",
        "context_markdown": "do the thing",
        "permission_mode": "acceptEdits",
        "ttl_seconds": 3600,
    }


@pytest.mark.asyncio
async def test_launch_goes_to_the_named_host_with_that_hosts_settings():
    launcher = launcher_for(
        theseus=SshTarget(host="10.0.0.7", user="bg", permission_mode="manual"),
        eyes=SshTarget(host="10.0.0.9", user="pi", port=2222, permission_mode="acceptEdits"),
    )
    fake_proc = make_fake_process(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)) as mock_exec:
        await launcher.launch(host="eyes", session_label="x", context_markdown="y")

    args, _kwargs = mock_exec.call_args
    assert args == ("ssh", "-o", "BatchMode=yes", "-p", "2222", "pi@10.0.0.9")
    assert json.loads(fake_proc.communicate.call_args.args[0])["permission_mode"] == "acceptEdits"
    assert launcher.hosts == ["theseus", "eyes"]


@pytest.mark.asyncio
async def test_launch_rejects_a_host_that_is_not_configured():
    launcher = launcher_for(theseus=SshTarget(host="h", user="u"))

    with pytest.raises(ValueError, match="Unknown host 'elsewhere'"):
        await launcher.launch(host="elsewhere", session_label="x", context_markdown="y")


@pytest.mark.asyncio
async def test_launch_raises_on_nonzero_exit():
    launcher = launcher_for(theseus=SshTarget(host="h", user="u"))
    fake_proc = make_fake_process(returncode=255, stderr=b"Host key verification failed.")

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)),
        pytest.raises(RuntimeError, match="Host key verification failed"),
    ):
        await launcher.launch(host="theseus", session_label="x", context_markdown="y")


@pytest.mark.asyncio
async def test_ttl_omitted_from_payload_when_not_configured():
    launcher = launcher_for(theseus=SshTarget(host="h", user="u"))
    fake_proc = make_fake_process(returncode=0)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        await launcher.launch(host="theseus", session_label="x", context_markdown="y")

    sent_payload = json.loads(fake_proc.communicate.call_args.args[0])
    assert "ttl_seconds" not in sent_payload


def test_build_launcher_returns_none_without_config():
    assert build_launcher(None) is None
    assert build_launcher({}) is None


def test_build_launcher_constructs_one_target_per_host_with_shared_defaults():
    launcher = build_launcher(
        {
            "type": "ssh",
            "identity_file": "/keys/k",
            "permission_mode": "acceptEdits",
            "ttl_seconds": 900,
            "hosts": {
                "theseus": {"host": "10.0.0.7", "user": "argus-bg"},
                "eyes": {"host": "10.0.0.9", "user": "pi", "ttl_seconds": 60},
            },
        }
    )

    assert isinstance(launcher, SshHostLauncher)
    assert launcher.hosts == ["theseus", "eyes"]
    theseus, eyes = launcher._targets["theseus"], launcher._targets["eyes"]
    assert (theseus.identity_file, theseus.permission_mode, theseus.ttl_seconds) == (
        "/keys/k", "acceptEdits", 900,
    )
    assert eyes.ttl_seconds == 60 and eyes.identity_file == "/keys/k"


def test_build_launcher_needs_at_least_one_host():
    with pytest.raises(ValueError, match="at least one entry under `hosts`"):
        build_launcher({"type": "ssh"})


def test_build_launcher_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unknown launcher type"):
        build_launcher({"type": "carrier-pigeon"})
