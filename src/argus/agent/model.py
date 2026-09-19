"""The chat model client: `ChatOpenAI` that also keeps a gateway-reported cost.

OpenRouter (and LiteLLM) return what a call actually cost as `usage.cost`. `ChatOpenAI` keeps
that field on a non-streaming response but drops it when it converts a streamed usage chunk
into token counts, and the agent streams. This subclass puts it back on the message so that
`argus.agent.tracing` can report it on the span. Upstream `langchain-openai` would be the
proper home for this.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


class CostReportingChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> Any:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        cost = (chunk.get("usage") or {}).get("cost")
        if generation is not None and isinstance(cost, int | float) and not isinstance(cost, bool):
            metadata = generation.message.response_metadata
            metadata["token_usage"] = {**metadata.get("token_usage", {}), "cost": cost}
        return generation
