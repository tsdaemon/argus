from __future__ import annotations

import httpx
import pytest

from argus.policy import PolicyEngine
from argus.providers.prometheus_provider import PrometheusProvider, _parse_duration


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
        raise AssertionError("READ tools never ask")


def make_tools(routes: dict[str, object], seen: list[httpx.Request] | None = None):
    """Register the provider's tools against a fake Prometheus answering `routes` by path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = routes[request.url.path]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json={"status": "success", "data": body})

    provider = PrometheusProvider(
        {"url": "http://prom:9090/"}, transport=httpx.MockTransport(handler)
    )
    mcp = FakeMCP()
    provider.register(mcp, PolicyEngine(NoApproval()), {})
    return mcp.registered


def test_requires_url():
    with pytest.raises(ValueError, match="url"):
        PrometheusProvider({})


def test_every_tool_is_registered_under_a_prefixed_name():
    tools = make_tools({})
    assert sorted(tools) == [
        "prometheus_alerts",
        "prometheus_label_values",
        "prometheus_list_metrics",
        "prometheus_query",
        "prometheus_query_range",
        "prometheus_targets",
    ]


@pytest.mark.asyncio
async def test_query_compacts_and_caps_series():
    series = [
        {"metric": {"__name__": "up", "job": f"j{i}"}, "value": [1_790_000_000, "1"]}
        for i in range(150)
    ]
    seen: list[httpx.Request] = []
    tools = make_tools({"/api/v1/query": {"resultType": "vector", "result": series}}, seen)

    result = await tools["prometheus_query"]("up", time="2026-10-02T08:00:00Z")

    assert seen[0].url.params["query"] == "up"
    assert seen[0].url.params["time"] == "2026-10-02T08:00:00Z"
    assert result["time"] == "2026-09-21T14:13:20Z"
    assert len(result["series"]) == 100
    assert result["series"][0] == {"labels": {"__name__": "up", "job": "j0"}, "value": "1"}
    assert "150" in result["truncated"]


@pytest.mark.asyncio
async def test_query_scalar():
    tools = make_tools({"/api/v1/query": {"resultType": "scalar", "result": [0, "42"]}})
    assert await tools["prometheus_query"]("42") == {"time": "1970-01-01T00:00:00Z", "value": "42"}


@pytest.mark.asyncio
async def test_query_error_returns_prometheus_message():
    error = httpx.Response(
        400, json={"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}
    )
    tools = make_tools({"/api/v1/query": error})
    with pytest.raises(ValueError, match="bad_data: parse error at char 3"):
        await tools["prometheus_query"]("up{")


@pytest.mark.asyncio
async def test_non_json_error_raises_http_error():
    tools = make_tools({"/api/v1/targets": httpx.Response(502, text="Bad Gateway")})
    with pytest.raises(httpx.HTTPStatusError):
        await tools["prometheus_targets"]()


@pytest.mark.asyncio
async def test_query_range_widens_step_and_aligns_values_on_grid():
    end = 1_790_000_000.0
    start = end - 86400
    seen: list[httpx.Request] = []
    matrix = {
        "resultType": "matrix",
        # The second step has no sample.
        "result": [{"metric": {"job": "a"}, "values": [[start, "1"], [start + 720, "3"]]}],
    }
    tools = make_tools({"/api/v1/query_range": matrix}, seen)

    result = await tools["prometheus_query_range"]("up", duration="1d", end=str(end), step="15s")

    # 86400 / 240 points = 360s, wider than the 15s asked for.
    assert seen[0].url.params["step"] == "360"
    assert result["step"] == "360s"
    assert result["start"] == "2026-09-20T14:13:20Z"
    values = result["series"][0]["values"]
    assert len(values) == 241
    assert values[:3] == ["1", None, "3"]


@pytest.mark.asyncio
async def test_query_range_rejects_bad_duration():
    tools = make_tools({})
    with pytest.raises(ValueError, match="Not a duration"):
        await tools["prometheus_query_range"]("up", duration="yesterday")


def test_parse_duration():
    assert _parse_duration("1h30m") == 5400
    assert _parse_duration("7d") == 604800
    with pytest.raises(ValueError):
        _parse_duration("5x")


@pytest.mark.asyncio
async def test_list_metrics_filters_and_adds_metadata():
    tools = make_tools(
        {
            "/api/v1/label/__name__/values": ["up", "smartprom_smart_passed", "zfs_arcstats_hits"],
            "/api/v1/metadata": {
                "smartprom_smart_passed": [{"type": "gauge", "help": "SMART passed", "unit": ""}]
            },
        }
    )
    result = await tools["prometheus_list_metrics"](match="SMART")
    assert result == {
        "count": 1,
        "metrics": [{"name": "smartprom_smart_passed", "type": "gauge", "help": "SMART passed"}],
    }


@pytest.mark.asyncio
async def test_list_metrics_asks_for_a_filter_when_too_many():
    names = [f"m{i}" for i in range(250)]
    tools = make_tools({"/api/v1/label/__name__/values": names})
    result = await tools["prometheus_list_metrics"]()
    assert result["count"] == 250
    assert len(result["names"]) == 200
    assert "match" in result["note"]


@pytest.mark.asyncio
async def test_label_values_passes_selector():
    seen: list[httpx.Request] = []
    tools = make_tools({"/api/v1/label/device/values": ["/dev/sda", "/dev/sdb"]}, seen)
    result = await tools["prometheus_label_values"]("device", match="smartprom_smart_passed")
    assert seen[0].url.params["match[]"] == "smartprom_smart_passed"
    assert result == {"count": 2, "values": ["/dev/sda", "/dev/sdb"]}


@pytest.mark.asyncio
async def test_targets():
    tools = make_tools(
        {
            "/api/v1/targets": {
                "activeTargets": [
                    {
                        "labels": {"job": "smartctl", "instance": "192.168.0.7:9902"},
                        "health": "down",
                        "lastError": "connection refused",
                        "lastScrape": "2026-10-02T08:00:00Z",
                    }
                ]
            }
        }
    )
    assert await tools["prometheus_targets"]() == [
        {
            "job": "smartctl",
            "instance": "192.168.0.7:9902",
            "health": "down",
            "last_error": "connection refused",
            "last_scrape": "2026-10-02T08:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_alerts_reports_rule_count():
    tools = make_tools(
        {
            "/api/v1/rules": {"groups": [{"rules": [{"name": "DiskHot"}, {"name": "Down"}]}]},
            "/api/v1/alerts": {
                "alerts": [
                    {
                        "labels": {"alertname": "DiskHot", "drive": "/dev/sda"},
                        "annotations": {"summary": "hot"},
                        "state": "firing",
                        "activeAt": "2026-10-02T07:00:00Z",
                        "value": "61",
                    }
                ]
            },
        }
    )
    result = await tools["prometheus_alerts"]()
    assert result["alerting_rules"] == 2
    assert result["alerts"][0]["name"] == "DiskHot"
    assert result["alerts"][0]["state"] == "firing"
