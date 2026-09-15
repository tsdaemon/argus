"""Best-effort notification channels — used to nudge a human towards something that
needs their attention out-of-band (currently: a pending break-glass request). Never
part of the security boundary itself: a failed or missing notifier must never block
or fake-approve anything, only mean the human finds out later than they'd like.
"""

from __future__ import annotations

from typing import Any, Protocol


class Notifier(Protocol):
    async def notify(self, *, title: str, message: str, url: str | None = None) -> None:
        """Best-effort push. Must never raise past the caller."""
        ...


class NullNotifier:
    """Default no-op notifier: used when a deployment hasn't configured one."""

    async def notify(self, *, title: str, message: str, url: str | None = None) -> None:
        return None


def build_notifier(config: dict[str, Any] | None) -> Notifier:
    """Construct the configured `Notifier`, or `NullNotifier` if none is set up."""
    if not config or not config.get("type"):
        return NullNotifier()

    notifier_type = config["type"]
    if notifier_type == "ntfy":
        from argus.notify.ntfy import NtfyNotifier

        return NtfyNotifier(
            topic_url=config["topic_url"],
            timeout=config.get("timeout", 10.0),
        )

    raise ValueError(f"Unknown notifier type: {notifier_type!r}")
