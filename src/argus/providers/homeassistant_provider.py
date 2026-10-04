"""Home Assistant over its REST API with a long-lived access token.

Reads (state, history, logbook, error log, services, templates, config check) are READ.
`ha.call_service` is classified per call from its domain and service (`classify_service`):
read-only services run, destructive ones are refused, everything else needs approval.
Results are compacted for a model, with caps and a note saying what was left out. HA's
errors come back as plain messages (a 401 says the token is invalid). The token is only
ever sent as a header: it appears in no result, error, or log line.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
from fastmcp import Context

from argus.policy import CallClassification, PolicyEngine, ToolClass
from argus.providers.base import ToolSpec, mcp_bind

_MAX_STATES = 200
_DEFAULT_STATES = 50
_MAX_HISTORY_POINTS = 100
_MAX_HISTORY_ENTITIES = 20
_MAX_LOGBOOK = 100
_MAX_LOG_LINES = 500
_DEFAULT_LOG_LINES = 100
_MAX_TEXT_CHARS = 8_000
_MAX_SERVICE_DOMAINS = 300

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SLUG = re.compile(r"^[a-z0-9_]+$")

# Services refused outright (see `classify_service`). `domain.service` entries are exact.
_DESTRUCTIVE_DOMAINS = {
    "hassio": "Supervisor control (host, add-ons, backups, updates); use an operator shell.",
    "shell_command": "Runs arbitrary shell commands inside HA; use the classified ssh tool.",
    "python_script": "Runs arbitrary code inside HA.",
    "pyscript": "Runs arbitrary code inside HA.",
}
_DESTRUCTIVE_SERVICES = {
    "homeassistant.restart": "Restarts Home Assistant.",
    "homeassistant.stop": "Stops Home Assistant.",
    "recorder.disable": "Stops recording history.",
}
_DESTRUCTIVE_NAME = re.compile(r"^(purge|delete|remove|factory_reset|wipe|erase)")


def classify_service(domain: str, service: str) -> CallClassification:
    """READ for services that only return data (`get_*`, such as `weather.get_forecasts`),
    DESTRUCTIVE for the short list above and any `purge*`/`delete*`/`remove*` service,
    MUTATE (approval) for everything else, including `homeassistant.reload_*`."""
    domain, service = domain.strip().lower(), service.strip().lower()
    if reason := _DESTRUCTIVE_DOMAINS.get(domain):
        return CallClassification(ToolClass.DESTRUCTIVE, f"`{domain}.*` is refused: {reason}")
    if reason := _DESTRUCTIVE_SERVICES.get(f"{domain}.{service}"):
        return CallClassification(ToolClass.DESTRUCTIVE, f"`{domain}.{service}`: {reason}")
    if _DESTRUCTIVE_NAME.match(service):
        return CallClassification(
            ToolClass.DESTRUCTIVE, f"`{domain}.{service}` deletes or purges data; refused."
        )
    if service.startswith("get_"):
        return CallClassification(ToolClass.READ, f"`{domain}.{service}` only returns data.")
    return CallClassification(
        ToolClass.MUTATE, f"`{domain}.{service}` can change the home; needs approval."
    )


class HomeAssistantProvider:
    name = "homeassistant"

    def __init__(
        self, config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        url = config.get("url")
        if not url:
            raise ValueError("The homeassistant provider needs a `url`.")
        token = config.get("token")
        if not token:
            raise ValueError(
                "The homeassistant provider needs a `token` (a long-lived access token; "
                "set it with `${ARGUS_HA_TOKEN}`)."
            )
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = float(config.get("timeout_seconds", 10))
        # Tests pass an `httpx.MockTransport`.
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._url,
                timeout=self._timeout,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as client:
                response = await client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            # Only the exception type: never echo anything that could carry request headers.
            raise ValueError(
                f"Could not reach Home Assistant at {self._url} ({type(exc).__name__})."
            ) from None
        if response.status_code == 401:
            raise ValueError(
                "Home Assistant rejected the token (401): the access token is invalid or "
                "revoked. Ask the operator to create a new long-lived token."
            )
        if response.status_code == 404:
            raise ValueError(f"Home Assistant returned 404 for {path}: {_message(response)}")
        if response.status_code >= 400:
            raise ValueError(
                f"Home Assistant rejected the request ({response.status_code}): "
                f"{_message(response)}"
            )
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise ValueError(f"Home Assistant returned a non-JSON response from {path}.") from None

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        async def get_state(entity_id: str) -> dict[str, Any]:
            """The state and attributes of one entity."""
            _check_entity(entity_id)
            data = await self._json("GET", f"/api/states/{entity_id}")
            return {
                "entity_id": data["entity_id"],
                "state": data["state"],
                "attributes": data.get("attributes", {}),
                "last_changed": data.get("last_changed"),
                "last_updated": data.get("last_updated"),
            }

        async def list_states(
            domain: str | None = None, search: str | None = None, limit: int = _DEFAULT_STATES
        ) -> dict[str, Any]:
            """List entities compactly, optionally by domain and a name/id substring."""
            states = await self._json("GET", "/api/states")
            if domain:
                states = [s for s in states if s["entity_id"].startswith(f"{domain.lower()}.")]
            if search:
                needle = search.lower()
                states = [
                    s
                    for s in states
                    if needle in s["entity_id"].lower()
                    or needle in str(s.get("attributes", {}).get("friendly_name", "")).lower()
                ]
            states.sort(key=lambda s: s["entity_id"])
            shown = max(1, min(limit, _MAX_STATES))
            result: dict[str, Any] = {
                "count": len(states),
                "entities": [
                    {
                        "entity_id": s["entity_id"],
                        "state": s["state"],
                        "friendly_name": s.get("attributes", {}).get("friendly_name"),
                        "last_changed": s.get("last_changed"),
                    }
                    for s in states[:shown]
                ],
            }
            _note_truncation(result, len(states), shown, "pass `domain` or `search`")
            return result

        async def get_history(
            entity_id: str, start: str, end: str | None = None
        ) -> dict[str, Any]:
            """State changes of one entity between `start` and `end` (default now)."""
            _check_entity(entity_id)
            params: dict[str, Any] = {
                "filter_entity_id": entity_id,
                "minimal_response": "",
                "no_attributes": "",
            }
            if end:
                params["end_time"] = _iso(end)
            data = await self._json("GET", f"/api/history/period/{_iso(start)}", params=params)
            changes = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
            result: dict[str, Any] = {
                "entity_id": entity_id,
                "count": len(changes),
                "changes": [
                    {"state": c.get("state"), "at": c.get("last_changed") or c.get("last_updated")}
                    for c in changes[-_MAX_HISTORY_POINTS:]
                    if isinstance(c, dict)
                ],
            }
            if len(changes) > _MAX_HISTORY_POINTS:
                result["truncated"] = (
                    f"Showing the latest {_MAX_HISTORY_POINTS} of {len(changes)} changes; "
                    "narrow `start`/`end` for the rest."
                )
            return result

        async def get_logbook(
            start: str, end: str | None = None, entity_id: str | None = None
        ) -> dict[str, Any]:
            """Logbook entries (what happened, in words) from `start` to `end`."""
            params: dict[str, Any] = {}
            if entity_id:
                _check_entity(entity_id)
                params["entity"] = entity_id
            if end:
                params["end_time"] = _iso(end)
            entries = await self._json("GET", f"/api/logbook/{_iso(start)}", params=params)
            if not isinstance(entries, list):
                entries = []
            result: dict[str, Any] = {
                "count": len(entries),
                "entries": [
                    {
                        k: e[k]
                        for k in ("when", "name", "message", "entity_id", "state", "domain")
                        if e.get(k) not in (None, "")
                    }
                    for e in entries[-_MAX_LOGBOOK:]
                    if isinstance(e, dict)
                ],
            }
            if len(entries) > _MAX_LOGBOOK:
                result["truncated"] = (
                    f"Showing the latest {_MAX_LOGBOOK} of {len(entries)} entries; narrow the "
                    "time range or pass `entity_id`."
                )
            return result

        async def get_error_log(lines: int = _DEFAULT_LOG_LINES) -> str:
            """The tail of home-assistant.log (warnings and errors)."""
            response = await self._request("GET", "/api/error_log")
            all_lines = response.text.splitlines()
            keep = max(1, min(lines, _MAX_LOG_LINES))
            text = "\n".join(all_lines[-keep:])
            if len(text) > _MAX_TEXT_CHARS:
                text = "[cut]\n" + text[-_MAX_TEXT_CHARS:]
            header = f"[last {min(keep, len(all_lines))} of {len(all_lines)} lines]"
            return f"{header}\n{text}"

        async def list_services(domain: str | None = None) -> dict[str, Any]:
            """Service names by domain; with `domain`, each service's description and fields."""
            data = await self._json("GET", "/api/services")
            if domain:
                entry = next((d for d in data if d["domain"] == domain.lower()), None)
                if entry is None:
                    raise ValueError(
                        f"No services for domain {domain!r}; domains: {[d['domain'] for d in data]}"
                    )
                return {
                    "domain": entry["domain"],
                    "services": {
                        name: {
                            "description": (svc.get("description") or "")[:200],
                            "fields": list(svc.get("fields") or {})[:30],
                        }
                        for name, svc in entry["services"].items()
                    },
                }
            result: dict[str, Any] = {
                "domains": {d["domain"]: sorted(d["services"]) for d in data[:_MAX_SERVICE_DOMAINS]}
            }
            _note_truncation(result, len(data), _MAX_SERVICE_DOMAINS, "pass `domain`")
            return result

        async def render_template(template: str) -> str:
            """Render a Jinja template on HA, e.g. `{{ states('sensor.x') }}`. Read-only."""
            response = await self._request("POST", "/api/template", json={"template": template})
            text = response.text
            if len(text) > _MAX_TEXT_CHARS:
                text = text[:_MAX_TEXT_CHARS] + f"\n[truncated to {_MAX_TEXT_CHARS} characters]"
            return text

        async def check_config() -> dict[str, Any]:
            """Validate configuration.yaml and included files without applying anything."""
            data = await self._json("POST", "/api/config/core/check_config")
            return {"result": data.get("result"), "errors": data.get("errors")}

        async def call_service(
            domain: str,
            service: str,
            ctx: Context,
            data: dict[str, Any] | None = None,
            target: dict[str, Any] | None = None,
        ) -> Any:
            """Call a Home Assistant service; classified per call from domain and service."""
            _check_slug(domain, "domain")
            _check_slug(service, "service")
            body = {**(data or {}), **(target or {})}
            params = {"return_response": "true"} if service.startswith("get_") else None
            response = await self._request(
                "POST", f"/api/services/{domain}/{service}", params=params, json=body
            )
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if params:
                text = repr(payload)
                if len(text) > _MAX_TEXT_CHARS:
                    return {"truncated": f"Response cut to {_MAX_TEXT_CHARS} characters.",
                            "response": text[:_MAX_TEXT_CHARS]}
                return payload
            changed = [
                {"entity_id": s["entity_id"], "state": s["state"]}
                for s in (payload if isinstance(payload, list) else [])[:50]
            ]
            return {"called": f"{domain}.{service}", "changed_states": changed}

        async def classify(args: dict[str, Any]) -> CallClassification:
            return classify_service(str(args.get("domain", "")), str(args.get("service", "")))

        time_help = "Times are RFC 3339 (`2026-10-02T08:00:00Z`) or Unix seconds."
        specs = [
            ToolSpec(
                tool_id="ha.get_state",
                tool_class=ToolClass.READ,
                summary="Get the state and attributes of one Home Assistant entity by `entity_id`.",
                fn=get_state,
            ),
            ToolSpec(
                tool_id="ha.list_states",
                tool_class=ToolClass.READ,
                summary=(
                    "List Home Assistant entities (entity_id, state, friendly_name, last_changed), "
                    f"filtered by `domain` (`light`) and/or a `search` substring; `limit` default "
                    f"{_DEFAULT_STATES}, at most {_MAX_STATES}. Says how many were left out."
                ),
                fn=list_states,
            ),
            ToolSpec(
                tool_id="ha.get_history",
                tool_class=ToolClass.READ,
                summary=(
                    "State changes of one entity from `start` to `end` (default now), as "
                    f"(state, time) pairs, latest {_MAX_HISTORY_POINTS}. {time_help}"
                ),
                fn=get_history,
            ),
            ToolSpec(
                tool_id="ha.get_logbook",
                tool_class=ToolClass.READ,
                summary=(
                    "Logbook entries from `start` to `end` (default now), optionally for one "
                    f"`entity_id`; latest {_MAX_LOGBOOK}. {time_help}"
                ),
                fn=get_logbook,
            ),
            ToolSpec(
                tool_id="ha.get_error_log",
                tool_class=ToolClass.READ,
                summary=(
                    f"The last `lines` (default {_DEFAULT_LOG_LINES}, at most {_MAX_LOG_LINES}) "
                    "of Home Assistant's error log."
                ),
                fn=get_error_log,
            ),
            ToolSpec(
                tool_id="ha.list_services",
                tool_class=ToolClass.READ,
                summary=(
                    "List service names by domain; pass `domain` for that domain's services with "
                    "descriptions and field names."
                ),
                fn=list_services,
            ),
            ToolSpec(
                tool_id="ha.render_template",
                tool_class=ToolClass.READ,
                summary=(
                    "Render a Jinja `template` on Home Assistant (`{{ states('sensor.x') }}`, "
                    "`{{ area_entities('kitchen') }}`). Templates only read state."
                ),
                fn=render_template,
            ),
            ToolSpec(
                tool_id="ha.check_config",
                tool_class=ToolClass.READ,
                summary="Validate Home Assistant's YAML configuration; applies nothing.",
                fn=check_config,
            ),
            ToolSpec(
                tool_id="ha.call_service",
                tool_class=ToolClass.MUTATE,
                summary=(
                    "Call a Home Assistant service: `domain`, `service`, optional `data` (service "
                    "fields) and `target` (`entity_id`, `device_id`, `area_id`). Each call is "
                    "risk-classified: `get_*` services that return data run at once, others need "
                    "operator approval, and restart/stop, Supervisor (`hassio.*`), purge/delete/"
                    "remove, and arbitrary-code services are refused. Find services with "
                    "`ha_list_services`."
                ),
                fn=call_service,
                classify=classify,
            ),
        ]
        return [replace(s, mcp_kwargs={"name": s.tool_id.replace(".", "_")}) for s in specs]

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        mcp_bind(mcp, policy, self.tool_specs(config))


def _check_entity(entity_id: str) -> None:
    if not _ENTITY_ID.match(entity_id):
        raise ValueError(f"Not an entity id: {entity_id!r}; expected `domain.object_id`.")


def _check_slug(value: str, what: str) -> None:
    if not _SLUG.match(value):
        raise ValueError(f"Not a valid service {what}: {value!r}.")


def _iso(text: str) -> str:
    """A time as the ISO string HA's history/logbook paths take; accepts Unix seconds."""
    try:
        return datetime.fromtimestamp(float(text), UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Not a time: {text!r}; use RFC 3339 or Unix seconds.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])[:300]
    return str(body)[:300]


def _note_truncation(result: dict[str, Any], total: int, shown: int, hint: str) -> None:
    if total > shown:
        result["truncated"] = f"Showing {shown} of {total}; {hint}."
