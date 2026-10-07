from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from argus.classifier import CachingClassifier
from argus.classifier.jev import JevClassifier
from argus.config import ArgusConfig, ProviderEntry
from argus.policy import CallClassification, ToolClass
from argus.providers.registry import instantiate_providers
from argus.providers.ssh_provider import SshProvider

HOSTS = {
    "router": {"host": "192.168.0.1", "port": 2222, "user": "admin", "description": "home router"},
    "nas": {"host": "192.0.2.7", "description": "NAS", "timeout_seconds": 0.01},
}


class FixedClassifier:
    def __init__(self, tool_class: ToolClass = ToolClass.READ) -> None:
        self.tool_class = tool_class
        self.seen: list[str] = []

    async def classify(self, command: str) -> CallClassification:
        self.seen.append(command)
        return CallClassification(self.tool_class, "fixed")


def fake_process(returncode=0, stdout=b"", stderr=b"", hang=False):
    """An ssh process: output readable at once; with `hang`, it only exits when killed."""
    proc = MagicMock()
    proc.returncode = None if hang else returncode
    killed = asyncio.Event()

    async def wait():
        if hang:
            await killed.wait()
            proc.returncode = -9
        return proc.returncode

    def kill():
        killed.set()

    proc.wait = wait
    proc.kill = MagicMock(side_effect=kill)
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.stderr.read = AsyncMock(return_value=stderr)
    return proc


def provider(**config) -> SshProvider:
    return SshProvider({"hosts": HOSTS, **config})


def run_spec(**config):
    (spec,) = provider(**config).tool_specs({})
    return spec


def test_needs_at_least_one_host():
    with pytest.raises(ValueError, match="hosts"):
        SshProvider({})


def test_one_tool_listing_every_host():
    spec = run_spec()

    assert spec.tool_id == "ssh.run"
    assert spec.mcp_kwargs == {"name": "ssh_run"}
    assert "`router`: home router" in spec.summary and "`nas`: NAS" in spec.summary


@pytest.mark.asyncio
async def test_classifies_with_that_hosts_classifier():
    router, nas = FixedClassifier(ToolClass.MUTATE), FixedClassifier(ToolClass.READ)
    (spec,) = SshProvider({"hosts": HOSTS}, classifiers={"router": router, "nas": nas}).tool_specs({})

    result = await spec.classify({"host": "router", "command": "service restart_wan"})

    assert result.tool_class is ToolClass.MUTATE
    assert router.seen == ["service restart_wan"] and nas.seen == []


@pytest.mark.asyncio
async def test_an_unknown_host_classifies_as_destructive():
    result = await run_spec().classify({"host": "mystery", "command": "ls"})

    assert result.tool_class is ToolClass.DESTRUCTIVE


def test_each_host_gets_its_description_as_classifier_context_and_can_override_it():
    p = provider(
        classifier={"type": "jev", "api_key": "k"},
        hosts={**HOSTS, "nas": {**HOSTS["nas"], "classifier": {"type": "ask"}}},
    )

    router = p._classifiers["router"]
    assert isinstance(router, CachingClassifier) and isinstance(router._inner, JevClassifier)
    assert router._inner._context == "host `router` (admin@192.168.0.1): home router"
    assert not isinstance(p._classifiers["nas"], CachingClassifier)


@pytest.mark.asyncio
async def test_runs_on_the_named_host_with_shared_and_own_settings():
    spec = run_spec(identity_file="/keys/argus_ssh", known_hosts_file="/keys/known_hosts")
    proc = fake_process(stdout=b"192.168.0.0/24 dev br0\r\n")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await spec.fn(host="router", command="ip route", ctx=None)

    assert mock_exec.call_args.args == (
        "ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR", "-p", "2222",
        "-i", "/keys/argus_ssh", "-o", "UserKnownHostsFile=/keys/known_hosts",
        "--", "admin@192.168.0.1", "ip route",
    )
    assert result == "exit 0\n192.168.0.0/24 dev br0\n"


@pytest.mark.asyncio
async def test_host_defaults_to_root_on_port_22():
    proc = fake_process()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        await run_spec().fn(host="nas", command="uptime", ctx=None)

    args = mock_exec.call_args.args
    assert args[args.index("-p") + 1] == "22"
    assert args[-2:] == ("root@192.0.2.7", "uptime")


@pytest.mark.asyncio
async def test_unknown_host_is_refused_by_the_tool_too():
    with pytest.raises(ValueError, match="Unknown host"):
        await run_spec().fn(host="mystery", command="ls", ctx=None)


@pytest.mark.asyncio
async def test_reports_exit_code_and_ssh_client_errors():
    proc = fake_process(returncode=255, stderr=b"Host key verification failed.\n")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_spec().fn(host="router", command="uptime", ctx=None)

    assert result.startswith("exit 255\n")
    assert "[stderr]\nHost key verification failed." in result


@pytest.mark.asyncio
async def test_truncates_long_output():
    proc = fake_process(stdout=b"x" * 50_000)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_spec().fn(host="router", command="cat big", ctx=None)

    assert len(result) < 16_100
    assert result.endswith("[truncated to 16000 characters]")


@pytest.mark.asyncio
async def test_a_command_past_its_timeout_is_stopped_and_returns_its_output_so_far():
    proc = fake_process(stdout=b"64 bytes from 192.168.0.2\r\n", hang=True)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_spec().fn(host="nas", command="ping 192.168.0.2", ctx=None)

    proc.kill.assert_called_once()
    assert result.startswith("Timed out after 0.01s; stopped on the host. Output so far:\n")
    assert "64 bytes from 192.168.0.2\n" in result


@pytest.mark.asyncio
async def test_a_stopped_run_stops_its_command():
    proc = fake_process(hang=True)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        call = asyncio.ensure_future(
            run_spec().fn(host="router", command="sleep 60", ctx=None, timeout_seconds=60)
        )
        await asyncio.sleep(0.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_a_call_can_ask_for_a_longer_timeout_up_to_the_hosts_maximum():
    spec = run_spec(timeout_seconds=0.01, max_timeout_seconds=0.05)
    proc = fake_process(hang=True)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await spec.fn(host="router", command="sleep 60", ctx=None, timeout_seconds=600)

    assert result.startswith("Timed out after 0.05s")


def test_registry_builds_it_from_config():
    config = ArgusConfig(providers={"ssh": ProviderEntry(enabled=True, hosts=HOSTS)})

    assert isinstance(instantiate_providers(config)["ssh"], SshProvider)


def test_command_uses_tty_by_default_and_omits_it_when_disabled():
    from argus.providers.ssh_provider import SshHost

    on = SshHost(name="router", host="10.0.0.1", description="d").command("uptime")
    off = SshHost(name="ha", host="10.0.0.2", description="d", port=22222, tty=False).command("uptime")
    assert "-tt" in on
    assert "-tt" not in off
    assert off[-3:] == ["--", "root@10.0.0.2", "uptime"]
    assert off[off.index("-p") + 1] == "22222"


def test_tty_false_in_config_reaches_the_host():
    provider = SshProvider(
        {"hosts": {"ha": {"host": "h", "description": "d", "tty": False}}},
        classifiers={"ha": MagicMock()},
    )
    assert provider._hosts["ha"].tty is False


@pytest.mark.asyncio
async def test_timeout_without_tty_does_not_promise_the_remote_command_stopped():
    proc = fake_process(stdout=b"partial\r\n", hang=True)
    hosts = {"ha": {"host": "h", "description": "d", "tty": False, "timeout_seconds": 0.01}}
    (spec,) = SshProvider({"hosts": hosts}, classifiers={"ha": MagicMock()}).tool_specs({})

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await spec.fn(host="ha", command="sleep 9", ctx=None)

    assert "stopped on the host" not in result
    assert "client stopped; the remote command may still be running" in result
    assert "partial" in result
