"""Jev, TypeSafe's decision model, through OpenRouter's Decisions API with the same key as
the chat models.

Jev answers a typed choice (read / mutate / destroy) with a probability for each. Only a
confident `read` becomes READ and only a confident `destroy` becomes DESTRUCTIVE; anything
in between is MUTATE, which policy turns into an approval request.
"""

from __future__ import annotations

from typing import Any

import httpx

from argus.classifier import CRITERIA, INSTRUCTIONS
from argus.policy import CallClassification, ToolClass

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class JevClassifier:
    def __init__(
        self,
        *,
        api_key: str,
        context: str,
        model: str = "typesafe/jev-1.13",
        url: str = DECISIONS_URL,
        read_threshold: float = 0.9,
        destructive_threshold: float = 0.8,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._context = context
        self._model = model
        self._url = url
        self._read_threshold = read_threshold
        self._destructive_threshold = destructive_threshold
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def classify(self, command: str) -> CallClassification:
        response = await self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "state": {"host": self._context, "command": command},
                "questions": {
                    "risk": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}
                },
            },
        )
        response.raise_for_status()
        answer: dict[str, Any] = response.json()["answers"]["risk"]["probabilities"]
        probabilities = {name: float(answer.get(name, 0.0)) for name in CRITERIA}
        return CallClassification(self._to_class(probabilities), _describe(probabilities))

    def _to_class(self, probabilities: dict[str, float]) -> ToolClass:
        if probabilities["read"] >= self._read_threshold:
            return ToolClass.READ
        if probabilities["destroy"] >= self._destructive_threshold:
            return ToolClass.DESTRUCTIVE
        return ToolClass.MUTATE


def _describe(probabilities: dict[str, float]) -> str:
    parts = " · ".join(f"{name} {probabilities[name]:.2f}" for name in CRITERIA)
    return f"Jev risk: {parts}"
