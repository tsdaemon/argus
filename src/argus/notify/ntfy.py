"""Push notifications via ntfy (https://ntfy.sh or a self-hosted instance).

Chosen as the v1 default because it needs no account and no API key — a POST to a
topic URL you pick yourself — and its phone app supports tapping a notification
straight through to a URL (`Click` header), which we point at the break-glass web
view. Treat your topic name like a shared secret: anyone who knows it can read your
notifications on the public ntfy.sh instance. Self-host ntfy, or use a private/
authenticated topic, for anything sensitive.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from argus.notify import Notifier


class NtfyNotifier(Notifier):
    def __init__(self, *, topic_url: str, timeout: float = 10.0) -> None:
        self._topic_url = topic_url
        self._timeout = timeout

    async def notify(self, *, title: str, message: str, url: str | None = None) -> None:
        try:
            await asyncio.to_thread(self._post, title, message, url)
        except Exception:  # noqa: BLE001, S110 — best-effort: a failed push must never break the caller
            pass

    def _post(self, title: str, message: str, url: str | None) -> None:
        headers = {"Title": title}
        if url:
            headers["Click"] = url
        request = urllib.request.Request(
            self._topic_url,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.URLError:
            pass
