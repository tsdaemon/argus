"""Prometheus: metric queries, metric discovery, scrape targets, and alerts, all READ.

Results are compacted for a model to read: labels and values only, timestamps as ISO
strings, and caps on series and points with a note saying what was left out. A query
Prometheus rejects comes back as its error message, so the model can fix its PromQL.
"""

from __future__ import annotations

import math
import re
import time as clock
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from argus.policy import PolicyEngine, ToolClass
from argus.providers.base import ToolSpec, mcp_bind

_MAX_INSTANT_SERIES = 100
_MAX_RANGE_SERIES = 20
_MAX_POINTS = 240
_MAX_NAMES = 200
_MAX_HELP_CHARS = 120

_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")


class PrometheusProvider:
    name = "prometheus"

    def __init__(
        self, config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        url = config.get("url")
        if not url:
            raise ValueError("The prometheus provider needs a `url`.")
        self._url = url.rstrip("/")
        self._timeout = float(config.get("timeout_seconds", 10))
        # Tests pass an `httpx.MockTransport`.
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(
            base_url=self._url, timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.get(path, params=params)
        # Prometheus explains a rejected query in a JSON body on a 4xx status.
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
            raise ValueError(f"Prometheus returned a non-JSON response from {path}.") from None
        if body.get("status") != "success":
            raise ValueError(f"Prometheus {body.get('errorType', 'error')}: {body.get('error')}")
        return body["data"]

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        async def query(query: str, time: str | None = None) -> dict[str, Any]:
            """Evaluate a PromQL expression at one instant."""
            params = {"query": query}
            if time:
                params["time"] = time
            data = await self._get("/api/v1/query", params)
            if data["resultType"] in ("scalar", "string"):
                ts, value = data["result"]
                return {"time": _iso(ts), "value": value}
            series = data["result"]
            result: dict[str, Any] = {
                "time": _iso(series[0]["value"][0]) if series else None,
                "series": [
                    {"labels": s["metric"], "value": s["value"][1]}
                    for s in series[:_MAX_INSTANT_SERIES]
                ],
            }
            _note_truncation(result, len(series), _MAX_INSTANT_SERIES)
            return result

        async def query_range(
            query: str, duration: str = "1h", end: str | None = None, step: str | None = None
        ) -> dict[str, Any]:
            """Evaluate a PromQL expression over a time range ending at `end` (default now)."""
            span = _parse_duration(duration)
            end_ts = _parse_time(end) if end else clock.time()
            start_ts = end_ts - span
            # Widen the step so no series has more than _MAX_POINTS points.
            step_s = max(math.ceil(span / _MAX_POINTS), 1)
            if step:
                step_s = max(_parse_duration(step), step_s)
            data = await self._get(
                "/api/v1/query_range",
                {"query": query, "start": start_ts, "end": end_ts, "step": step_s},
            )
            series = data["result"]
            count = math.floor(span / step_s) + 1
            result: dict[str, Any] = {
                "start": _iso(start_ts),
                "end": _iso(end_ts),
                "step": f"{step_s:g}s",
                "series": [
                    {"labels": s["metric"], "values": _on_grid(s["values"], start_ts, step_s, count)}
                    for s in series[:_MAX_RANGE_SERIES]
                ],
            }
            _note_truncation(result, len(series), _MAX_RANGE_SERIES)
            return result

        async def list_metrics(match: str | None = None) -> dict[str, Any]:
            """List metric names, with type and help text where known."""
            names = await self._get("/api/v1/label/__name__/values")
            if match:
                names = [n for n in names if match.lower() in n.lower()]
            if len(names) > _MAX_NAMES:
                return {
                    "count": len(names),
                    "note": f"Too many to list; pass a more specific `match`. First {_MAX_NAMES}:",
                    "names": names[:_MAX_NAMES],
                }
            metadata = await self._get("/api/v1/metadata")
            metrics = []
            for name in names:
                entry: dict[str, Any] = {"name": name}
                if meta := metadata.get(name):
                    entry["type"] = meta[0].get("type")
                    if help_text := meta[0].get("help"):
                        entry["help"] = help_text[:_MAX_HELP_CHARS]
                metrics.append(entry)
            return {"count": len(metrics), "metrics": metrics}

        async def label_values(label: str, match: str | None = None) -> dict[str, Any]:
            """List the values a label takes, optionally only on series matching `match`."""
            params = {"match[]": match} if match else None
            values = await self._get(f"/api/v1/label/{label}/values", params)
            result: dict[str, Any] = {"count": len(values), "values": values[:_MAX_NAMES]}
            _note_truncation(result, len(values), _MAX_NAMES)
            return result

        async def targets() -> list[dict[str, Any]]:
            """List active scrape targets and their health."""
            data = await self._get("/api/v1/targets", {"state": "active"})
            return [
                {
                    "job": t["labels"].get("job"),
                    "instance": t["labels"].get("instance"),
                    "health": t["health"],
                    "last_error": t.get("lastError") or None,
                    "last_scrape": t.get("lastScrape"),
                }
                for t in data["activeTargets"]
            ]

        async def alerts() -> dict[str, Any]:
            """List pending and firing alerts, and how many alerting rules exist."""
            rules = await self._get("/api/v1/rules", {"type": "alert"})
            data = await self._get("/api/v1/alerts")
            return {
                "alerting_rules": sum(len(g["rules"]) for g in rules["groups"]),
                "alerts": [
                    {
                        "name": a["labels"].get("alertname"),
                        "state": a["state"],
                        "labels": a["labels"],
                        "annotations": a.get("annotations", {}),
                        "active_at": a.get("activeAt"),
                        "value": a.get("value"),
                    }
                    for a in data["alerts"]
                ],
            }

        time_help = "Times are RFC 3339 (`2026-10-02T08:00:00Z`) or Unix seconds."
        duration_help = "Durations are Prometheus style: `30m`, `6h`, `7d`, `1h30m`."
        specs = [
            ToolSpec(
                tool_id="prometheus.query",
                tool_class=ToolClass.READ,
                summary=(
                    "Evaluate a PromQL `query` at one instant: `time`, or now if omitted. "
                    f"{time_help} Returns each series' labels and value, at most "
                    f"{_MAX_INSTANT_SERIES} series; aggregate (`sum by`, `topk`) to see more."
                ),
                fn=query,
            ),
            ToolSpec(
                tool_id="prometheus.query_range",
                tool_class=ToolClass.READ,
                summary=(
                    "Evaluate a PromQL `query` over the `duration` (default `1h`) ending at "
                    f"`end` (default now). {time_help} {duration_help} `step` is optional; it "
                    f"is widened so a series has at most {_MAX_POINTS} points. Each series' "
                    "`values` are on the grid start, start+step, ..., with null where there was "
                    f"no sample; at most {_MAX_RANGE_SERIES} series."
                ),
                fn=query_range,
            ),
            ToolSpec(
                tool_id="prometheus.list_metrics",
                tool_class=ToolClass.READ,
                summary=(
                    "List metric names with their type and help text. `match` filters by a "
                    "case-insensitive substring of the name (`smart`, `zfs`, `docker`). Use it to "
                    "find a metric's name before querying it."
                ),
                fn=list_metrics,
            ),
            ToolSpec(
                tool_id="prometheus.label_values",
                tool_class=ToolClass.READ,
                summary=(
                    "List the values of a `label` (such as `job`, `instance`, `device`). `match` "
                    "is an optional series selector, such as `smartprom_smart_passed` or "
                    "`docker_container_mem_usage{container_name=~\"argus.*\"}`, limiting it to "
                    "those series."
                ),
                fn=label_values,
            ),
            ToolSpec(
                tool_id="prometheus.targets",
                tool_class=ToolClass.READ,
                summary="List Prometheus's scrape targets with health, last error, and last scrape.",
                fn=targets,
            ),
            ToolSpec(
                tool_id="prometheus.alerts",
                tool_class=ToolClass.READ,
                summary=(
                    "List pending and firing alerts, with the number of alerting rules; with no "
                    "alerting rules, an empty list says nothing about health."
                ),
                fn=alerts,
            ),
        ]
        # The bare function names (`query`, `targets`) would be ambiguous among MCP tools.
        return [replace(s, mcp_kwargs={"name": s.tool_id.replace(".", "_")}) for s in specs]

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        mcp_bind(mcp, policy, self.tool_specs(config))


def _parse_duration(text: str) -> float:
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.strip():
        raise ValueError(f"Not a duration: {text!r}; use e.g. `30m`, `6h`, `7d`, `1h30m`.")
    seconds = sum(int(n) * _DURATION_UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise ValueError(f"Duration must be positive: {text!r}.")
    return seconds


def _parse_time(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Not a time: {text!r}; use RFC 3339 or Unix seconds.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).isoformat().replace("+00:00", "Z")


def _on_grid(values: list[list[Any]], start: float, step: float, count: int) -> list[str | None]:
    """Prometheus evaluates a range query at start + k*step and omits steps with no sample."""
    grid: list[str | None] = [None] * count
    for ts, value in values:
        k = round((float(ts) - start) / step)
        if 0 <= k < count:
            grid[k] = value
    return grid


def _note_truncation(result: dict[str, Any], total: int, shown: int) -> None:
    if total > shown:
        result["truncated"] = f"Showing {shown} of {total}; narrow the query or aggregate."
