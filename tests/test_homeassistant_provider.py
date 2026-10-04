from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from argus.agent.tools import classification_middleware, langchain_bind
from argus.config import ArgusConfig, ProviderEntry, load_config
from argus.policy import PolicyEngine, ToolClass
from argus.providers.homeassistant_provider import HomeAssistantProvider, classify_service
from argus.providers.registry import instantiate_providers

TOKEN = "sekret-token-123"
EXAMPLES = Path(__file__).parent.parent / "examples"


class FakeMCP:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.registered[kwargs.get("name", fn.__name__)] = fn
            return fn

        return decorator


class NoApproval:
    async def request(self, ctx, *, action, summary, details) -> bool:
        raise AssertionError("tests call the tool functions through ToolSpec, not the gate")


def make(handler):
    provider = HomeAssistantProvider(
        {"url": "http://ha:8123/", "token": TOKEN}, transport=httpx.MockTransport(handler)
    )
    return provider, {s.tool_id: s for s in provider.tool_specs({})}


def json_handler(routes: dict[tuple[str, str], object], seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = routes[(request.method, request.url.path)]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return handler


def test_requires_url_and_token():
    with pytest.raises(ValueError, match="url"):
        HomeAssistantProvider({"token": "t"})
    with pytest.raises(ValueError, match="token"):
        HomeAssistantProvider({"url": "http://ha", "token": ""})


def test_registered_names_and_classes():
    _, specs = make(json_handler({}))
    assert sorted(s.mcp_kwargs["name"] for s in specs.values()) == [
        "ha_call_service", "ha_check_config", "ha_get_error_log", "ha_get_history",
        "ha_get_logbook", "ha_get_state", "ha_list_services", "ha_list_states",
        "ha_render_template",
    ]
    assert specs["ha.call_service"].classify is not None
    assert all(s.tool_class is ToolClass.READ for k, s in specs.items() if k != "ha.call_service")


@pytest.mark.asyncio
async def test_get_state_sends_bearer_and_never_leaks_it():
    seen: list[httpx.Request] = []
    state = {"entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": 200},
             "last_changed": "2026-10-04T08:00:00+00:00", "last_updated": "x", "context": {}}
    _, specs = make(json_handler({("GET", "/api/states/light.kitchen"): state}, seen))

    result = await specs["ha.get_state"].fn(entity_id="light.kitchen")

    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert result["state"] == "on" and "context" not in result
    assert TOKEN not in json.dumps(result)


@pytest.mark.asyncio
async def test_list_states_filters_and_notes_truncation():
    states = [
        {"entity_id": f"light.l{i:02}", "state": "on", "attributes": {"friendly_name": f"Lamp {i}"},
         "last_changed": "t"}
        for i in range(30)
    ] + [{"entity_id": "sensor.t", "state": "20", "attributes": {}, "last_changed": "t"}]
    _, specs = make(json_handler({("GET", "/api/states"): states}))

    result = await specs["ha.list_states"].fn(domain="light", limit=5)

    assert result["count"] == 30 and len(result["entities"]) == 5
    assert "Showing 5 of 30" in result["truncated"]
    assert set(result["entities"][0]) == {"entity_id", "state", "friendly_name", "last_changed"}
    found = await specs["ha.list_states"].fn(search="lamp 7")
    assert [e["entity_id"] for e in found["entities"]] == ["light.l07"]


@pytest.mark.asyncio
async def test_history_is_compact_and_capped():
    changes = [{"state": str(i), "last_changed": f"t{i}"} for i in range(150)]
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[changes])

    _, specs = make(handler)
    result = await specs["ha.get_history"].fn(entity_id="sensor.t", start="2026-10-04T00:00:00Z")

    assert len(result["changes"]) == 100 and "latest 100 of 150" in result["truncated"]
    assert seen[0].url.params["filter_entity_id"] == "sensor.t"
    assert "minimal_response" in seen[0].url.params


@pytest.mark.asyncio
async def test_error_log_tail():
    text = "\n".join(f"line {i}" for i in range(300))
    _, specs = make(lambda r: httpx.Response(200, text=text))
    out = await specs["ha.get_error_log"].fn(lines=3)
    assert out.endswith("line 297\nline 298\nline 299") and "last 3 of 300" in out


@pytest.mark.asyncio
async def test_template_and_check_config_and_services():
    routes = {
        ("POST", "/api/template"): httpx.Response(200, text="21.5"),
        ("POST", "/api/config/core/check_config"): {"result": "valid", "errors": None},
        ("GET", "/api/services"): [{"domain": "light", "services": {"turn_on": {
            "description": "Turn on", "fields": {"brightness": {}}}}}],
    }
    _, specs = make(json_handler(routes))
    assert await specs["ha.render_template"].fn(template="{{ 1 }}") == "21.5"
    assert (await specs["ha.check_config"].fn())["result"] == "valid"
    assert (await specs["ha.list_services"].fn())["domains"] == {"light": ["turn_on"]}
    detail = await specs["ha.list_services"].fn(domain="light")
    assert detail["services"]["turn_on"]["fields"] == ["brightness"]


@pytest.mark.asyncio
async def test_call_service_posts_data_and_target():
    seen: list[httpx.Request] = []
    _, specs = make(json_handler(
        {("POST", "/api/services/light/turn_on"): [{"entity_id": "light.k", "state": "on"}]}, seen
    ))

    result = await specs["ha.call_service"].fn(
        domain="light", service="turn_on", ctx=None,
        data={"brightness": 100}, target={"entity_id": "light.k"},
    )

    assert json.loads(seen[0].content) == {"brightness": 100, "entity_id": "light.k"}
    assert result["changed_states"] == [{"entity_id": "light.k", "state": "on"}]


@pytest.mark.asyncio
async def test_get_service_asks_for_the_response():
    seen: list[httpx.Request] = []
    _, specs = make(json_handler(
        {("POST", "/api/services/weather/get_forecasts"): {"service_response": {"w.x": {}}}}, seen
    ))
    result = await specs["ha.call_service"].fn(domain="weather", service="get_forecasts", ctx=None)
    assert seen[0].url.params["return_response"] == "true"
    assert "service_response" in result


@pytest.mark.asyncio
async def test_path_injection_is_refused():
    _, specs = make(json_handler({}))
    with pytest.raises(ValueError):
        await specs["ha.call_service"].fn(domain="../api", service="x", ctx=None)
    with pytest.raises(ValueError):
        await specs["ha.get_state"].fn(entity_id="light.k/../../config")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "token is invalid"), (404, "404"), (400, "bad thing")],
)
async def test_errors_are_plain_messages_without_the_token(status, needle):
    _, specs = make(lambda r: httpx.Response(status, json={"message": "bad thing"}))
    with pytest.raises(ValueError, match=needle) as exc:
        await specs["ha.get_state"].fn(entity_id="light.k")
    assert TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_connection_error_hides_details():
    def handler(request):
        raise httpx.ConnectError(f"boom {request.headers['authorization']}")

    _, specs = make(handler)
    with pytest.raises(ValueError, match="Could not reach") as exc:
        await specs["ha.get_state"].fn(entity_id="light.k")
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)


@pytest.mark.parametrize(
    ("domain", "service", "expected"),
    [
        ("weather", "get_forecasts", ToolClass.READ),
        ("calendar", "get_events", ToolClass.READ),
        ("light", "turn_on", ToolClass.MUTATE),
        ("script", "turn_off", ToolClass.MUTATE),
        ("homeassistant", "reload_all", ToolClass.MUTATE),
        ("homeassistant", "restart", ToolClass.DESTRUCTIVE),
        ("homeassistant", "stop", ToolClass.DESTRUCTIVE),
        ("hassio", "addon_stop", ToolClass.DESTRUCTIVE),
        ("hassio", "host_reboot", ToolClass.DESTRUCTIVE),
        ("recorder", "purge", ToolClass.DESTRUCTIVE),
        ("recorder", "purge_entities", ToolClass.DESTRUCTIVE),
        ("shell_command", "anything", ToolClass.DESTRUCTIVE),
        ("persistent_notification", "dismiss", ToolClass.MUTATE),
        ("foo", "delete_user", ToolClass.DESTRUCTIVE),
        ("foo", "remove_device", ToolClass.DESTRUCTIVE),
        ("Light", " Turn_On ", ToolClass.MUTATE),
    ],
)
def test_service_classification(domain, service, expected):
    assert classify_service(domain, service).tool_class is expected


@pytest.mark.asyncio
async def test_spec_classify_uses_call_arguments():
    _, specs = make(json_handler({}))
    result = await specs["ha.call_service"].classify({"domain": "hassio", "service": "x"})
    assert result.tool_class is ToolClass.DESTRUCTIVE and "hassio" in result.note


def test_agent_binding_gets_per_call_hitl_and_classification():
    provider, _ = make(json_handler({}))
    specs = provider.tool_specs({})
    policy = PolicyEngine(None)

    tools, interrupt_on = langchain_bind(policy, specs)

    assert "ha_call_service" in {t.name for t in tools}
    assert list(interrupt_on) == ["ha_call_service"]  # reads never interrupt
    middleware = classification_middleware(policy, specs)
    assert middleware is not None and set(middleware._specs) == {"ha_call_service"}


def test_mcp_binding_registers_every_tool():
    provider, _ = make(json_handler({}))
    mcp = FakeMCP()
    provider.register(mcp, PolicyEngine(NoApproval()), {})
    assert "ha_call_service" in mcp.registered and len(mcp.registered) == 9


def test_registry_builds_it_from_config():
    config = ArgusConfig(providers={"homeassistant": ProviderEntry(
        enabled=True, url="http://ha:8123", token="t")})
    assert isinstance(instantiate_providers(config)["homeassistant"], HomeAssistantProvider)


@pytest.mark.parametrize("name", ["theseus.argus.yaml", "argus.dev.yaml"])
def test_example_configs_have_the_ha_host_and_provider(name, monkeypatch):
    for var in ("ARGUS_ROUTER_SSH_PORT", "THESEUS_IP", "ARGUS_HA_TOKEN", "OPENROUTER_API_KEY",
                "ARGUS_AGENT_DATABASE_URL", "ARGUS_NTFY_TOPIC_URL", "ARGUS_APPROVAL_URL"):
        monkeypatch.setenv(var, "x" if var != "ARGUS_ROUTER_SSH_PORT" else "22")
    config = load_config(EXAMPLES / name)

    host = config.providers["ssh"].model_extra["hosts"]["homeassistant"]
    assert (host["host"], host["port"], host["user"]) == ("192.168.0.100", 22222, "root")
    assert "ha" in host["description"] and "/config" in host["description"]
    ha = config.providers["homeassistant"]
    assert ha.model_extra["url"] == "http://192.168.0.100"
    assert ha.model_extra["token"] == "x"


@pytest.mark.parametrize(
    ("domain", "service"),
    [
        ("python_script", "run"),
        ("pyscript", "reload"),
        ("foo", "factory_reset"),
        ("foo", "wipe_all"),
        ("foo", "erase_data"),
        ("recorder", "disable"),
    ],
)
def test_more_destructive_services(domain, service):
    assert classify_service(domain, service).tool_class is ToolClass.DESTRUCTIVE


@pytest.mark.asyncio
async def test_destructive_call_is_refused_on_mcp():
    provider, _ = make(json_handler({}))
    mcp = FakeMCP()
    provider.register(mcp, PolicyEngine(NoApproval()), {})

    with pytest.raises(PermissionError, match="ha.call_service"):
        await mcp.registered["ha_call_service"](domain="homeassistant", service="restart")


@pytest.mark.asyncio
async def test_destructive_call_is_refused_by_the_agent_middleware():
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    provider, _ = make(json_handler({}))
    specs = provider.tool_specs({})
    middleware = classification_middleware(PolicyEngine(None), specs)
    call = {"name": "ha_call_service", "id": "c1", "args": {"domain": "hassio", "service": "x"}}
    message = AIMessage(content="", tool_calls=[call])
    await middleware._classify(message)

    async def handler(request):
        raise AssertionError("a refused call must not run")

    request = SimpleNamespace(tool_call=call, state={"messages": [message]})
    result = await middleware.awrap_tool_call(request, handler)

    assert result.status == "error" and "Refused" in result.content


@pytest.mark.asyncio
async def test_logbook_is_compact_and_capped():
    entries = [{"when": f"t{i}", "name": "Kitchen", "message": "turned on", "context_id": "zz",
                "entity_id": "light.kitchen"} for i in range(120)]
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=entries)

    _, specs = make(handler)
    result = await specs["ha.get_logbook"].fn(
        start="2026-10-04T00:00:00Z", entity_id="light.kitchen"
    )

    assert result["count"] == 120 and len(result["entries"]) == 100
    assert "context_id" not in result["entries"][0] and "latest 100 of 120" in result["truncated"]
    assert seen[0].url.params["entity"] == "light.kitchen"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["1e300", "inf", "nan", "yesterday"])
async def test_invalid_times_are_plain_errors(bad):
    _, specs = make(json_handler({}))
    with pytest.raises(ValueError, match="Not a time"):
        await specs["ha.get_history"].fn(entity_id="sensor.t", start=bad)
    with pytest.raises(ValueError, match="Not a time"):
        await specs["ha.get_logbook"].fn(start=bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], [[]], {"message": "x"}, "null"])
async def test_history_survives_unexpected_shapes(body):
    _, specs = make(lambda request: httpx.Response(200, json=body))
    result = await specs["ha.get_history"].fn(entity_id="sensor.t", start="2026-10-04T00:00:00Z")
    assert result["count"] == 0 and result["changes"] == []


@pytest.mark.parametrize("name", ["theseus.argus.yaml", "argus.dev.yaml"])
def test_example_ha_host_has_tty_disabled(name, monkeypatch):
    for var in ("ARGUS_ROUTER_SSH_PORT", "THESEUS_IP", "ARGUS_HA_TOKEN", "OPENROUTER_API_KEY",
                "ARGUS_AGENT_DATABASE_URL", "ARGUS_NTFY_TOPIC_URL", "ARGUS_APPROVAL_URL"):
        monkeypatch.setenv(var, "x" if var != "ARGUS_ROUTER_SSH_PORT" else "22")
    hosts = load_config(EXAMPLES / name).providers["ssh"].model_extra["hosts"]
    assert hosts["homeassistant"]["tty"] is False
    assert "tty" not in hosts["router"]
