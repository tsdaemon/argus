"""Per-call risk classification of a shell command, for a tool whose risk depends on its
arguments (`ssh.run`).

A classifier returns a `ToolClass` (READ / MUTATE / DESTRUCTIVE); it never decides what is
allowed. `PolicyEngine.decide()` maps that class to run / ask / refuse, as it does for a
tool's static class. Which classifier runs is the `type` in the provider's `classifier`
config block:

- `jev`: TypeSafe's Jev decision model through OpenRouter's Decisions API; calibrated
  probabilities, compared against thresholds (`argus.classifier.jev`).
- `llm`: any OpenRouter chat model with structured output (`argus.classifier.llm`).
- `ask`: no classifier; every call is MUTATE, so every call needs approval. The default
  when no `classifier` block is configured.

Every remote classifier is wrapped in `CachingClassifier`: answers are cached by command,
a failure is retried once, and a second failure becomes an uncached MUTATE, so an outage
means asking, never running unseen.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Protocol

from argus.policy import CallClassification, ToolClass

CRITERIA = {
    "read": "Only reads or displays state; changes nothing.",
    "mutate": "Changes configuration, files, or services in a recoverable way.",
    "destroy": "Deletes data, erases flash or nvram, bricks the device, or locks out access.",
}
INSTRUCTIONS = (
    "What effect would running this exact shell command have? Consider hidden effects: "
    "decoded or evaluated strings, -exec, chained commands, and scripts it runs."
)


class CommandClassifier(Protocol):
    async def classify(self, command: str) -> CallClassification:
        """The class of `command`. Implementations may raise; `CachingClassifier` turns
        that into MUTATE."""
        ...


class AskClassifier:
    """Classifies nothing: every command needs approval."""

    async def classify(self, command: str) -> CallClassification:
        return CallClassification(ToolClass.MUTATE, "No classifier configured; every command is asked.")


class CachingClassifier:
    def __init__(self, inner: CommandClassifier, *, cache_size: int = 256) -> None:
        self._inner = inner
        self._cache: OrderedDict[str, CallClassification] = OrderedDict()
        self._cache_size = cache_size

    async def classify(self, command: str) -> CallClassification:
        if command in self._cache:
            self._cache.move_to_end(command)
            return self._cache[command]
        # One retry: a dropped request or a slow answer is usually transient.
        for attempt in range(2):
            try:
                result = await self._inner.classify(command)
                break
            except Exception as exc:  # noqa: BLE001 - any failure means "ask a human"
                if attempt == 1:
                    # Timeouts have an empty message, so name the error type too.
                    reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    return CallClassification(ToolClass.MUTATE, f"Risk classifier unavailable ({reason}).")

        self._cache[command] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result


def build_classifier(config: dict[str, Any] | None, *, context: str) -> CommandClassifier:
    """The classifier a `classifier` config block names. `context` describes the host the
    commands run on (e.g. "Asuswrt-Merlin router, root shell over SSH")."""
    settings = dict(config or {})
    classifier_type = settings.pop("type", "ask")
    cache_size = settings.pop("cache_size", 256)

    if classifier_type == "ask":
        return AskClassifier()
    if classifier_type == "jev":
        from argus.classifier.jev import JevClassifier

        return CachingClassifier(JevClassifier(context=context, **settings), cache_size=cache_size)
    if classifier_type == "llm":
        from argus.classifier.llm import LlmClassifier

        return CachingClassifier(LlmClassifier(context=context, **settings), cache_size=cache_size)

    raise ValueError(f"Unknown classifier type: {classifier_type!r} (expected jev, llm, or ask)")
