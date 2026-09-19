"""OpenTelemetry/OpenInference tracing to Phoenix.

Tracing is never on the correctness path: `setup_tracing` swallows every failure, and spans
are exported from a background thread, so an unreachable collector cannot fail or slow a run.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from argus.config import OtelConfig

logger = logging.getLogger(__name__)


def _cost_in(usage: Any) -> float | None:
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost) if math.isfinite(cost) and cost >= 0 else None


def reported_cost(outputs: Any) -> float | None:
    """The cost a gateway (OpenRouter, LiteLLM) reported as `usage.cost`, from a LangChain run.

    Non-streaming runs carry it in `llm_output.token_usage`; streamed runs carry it on the
    message's `response_metadata.token_usage` (see `argus.agent.model`). Written to be liftable
    into `openinference-instrumentation-langchain`, which does not report cost.
    """
    if not isinstance(outputs, Mapping):
        return None
    llm_output = outputs.get("llm_output")
    if (
        isinstance(llm_output, Mapping)
        and (cost := _cost_in(llm_output.get("token_usage"))) is not None
    ):
        return cost
    for generations in outputs.get("generations") or []:
        for generation in generations:
            message = generation.get("message") if isinstance(generation, Mapping) else None
            kwargs = message.get("kwargs") if isinstance(message, Mapping) else None
            metadata = kwargs.get("response_metadata") if isinstance(kwargs, Mapping) else None
            usage = metadata.get("token_usage") if isinstance(metadata, Mapping) else None
            if (cost := _cost_in(usage)) is not None:
                return cost
    return None


def _report_provider_cost() -> None:
    """Have the instrumentor set `llm.cost.total` on LLM spans that carry a reported cost.

    The instrumentor has no hook for extra attributes, so wrap its private `_update_span`
    (openinference-instrumentation-langchain 0.1.x). Idempotent.
    """
    from openinference.instrumentation.langchain import _tracer
    from openinference.semconv.trace import SpanAttributes

    original = _tracer._update_span
    if getattr(original, "_argus_reports_cost", False):
        return

    def update_span(span: Any, run: Any) -> None:
        original(span, run)
        try:
            if run.run_type == "llm" and (cost := reported_cost(run.outputs)) is not None:
                span.set_attribute(SpanAttributes.LLM_COST_TOTAL, cost)
        except Exception:
            logger.debug("Could not read a reported cost from the run", exc_info=True)

    update_span._argus_reports_cost = True  # type: ignore[attr-defined]
    _tracer._update_span = update_span


def _start(config: OtelConfig) -> None:
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from phoenix.otel import register

    provider = register(
        endpoint=config.endpoint,
        project_name=config.project_name,
        batch=True,
        verbose=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=provider)
    _report_provider_cost()


def setup_tracing(config: OtelConfig) -> bool:
    """Instrument LangChain/LangGraph when enabled; return whether tracing is active."""
    if not config.enabled:
        return False
    try:
        _start(config)
    except Exception:
        logger.warning("Tracing setup failed; continuing without traces", exc_info=True)
        return False
    logger.info("Tracing to %s (project %s)", config.endpoint, config.project_name)
    return True
