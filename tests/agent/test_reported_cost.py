"""OpenRouter-style `usage.cost` reaching the trace, without any network."""

import json

import httpx
import pytest
from openinference.instrumentation.langchain import LangChainInstrumentor, _tracer
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from argus.agent import tracing
from argus.agent.model import CostReportingChatOpenAI
from argus.agent.tracing import reported_cost


def usage(cost=0.0123):
    body = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    return {**body, "cost": cost} if cost is not None else body


def gateway(cost=0.0123):
    """A fake OpenRouter: one JSON reply, or an SSE stream ending in a usage chunk."""

    def handler(request: httpx.Request) -> httpx.Response:
        base = {"id": "gen-1", "created": 1, "model": "anthropic/claude-4.5-sonnet"}
        if json.loads(request.content).get("stream"):
            chunks = [
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}}],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {**base, "choices": [], "usage": usage(cost)},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
        message = {"role": "assistant", "content": "hi"}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        return httpx.Response(200, json={**base, "choices": [choice], "usage": usage(cost)})

    return httpx.MockTransport(handler)


def model(cost=0.0123):
    return CostReportingChatOpenAI(
        model="anthropic/claude-sonnet-4.5",
        base_url="http://gateway.test/v1",
        api_key="test",
        http_client=httpx.Client(transport=gateway(cost)),
        http_async_client=httpx.AsyncClient(transport=gateway(cost)),
    )


@pytest.fixture
def spans(monkeypatch):
    """LangChain instrumented onto an in-memory exporter, with argus's cost hook installed."""
    monkeypatch.setattr(_tracer, "_update_span", _tracer._update_span)  # restored on teardown
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    LangChainInstrumentor().instrument(tracer_provider=provider)
    tracing._report_provider_cost()
    yield exporter
    LangChainInstrumentor().uninstrument()


def llm_costs(exporter):
    return [
        s.attributes.get("llm.cost.total")
        for s in exporter.get_finished_spans()
        if s.attributes.get("openinference.span.kind") == "LLM"
    ]


def test_streamed_message_keeps_the_gateway_cost():
    async def collect():
        final = None
        async for chunk in model().astream("hi"):
            final = chunk if final is None else final + chunk
        return final

    import asyncio

    final = asyncio.run(collect())

    assert final.response_metadata["token_usage"]["cost"] == 0.0123
    assert final.usage_metadata["total_tokens"] == 1200


def test_non_streaming_call_is_reported_on_its_span(spans):
    model().invoke("hi")

    assert llm_costs(spans) == [0.0123]


async def test_streaming_call_is_reported_on_its_span(spans):
    async for _ in model().astream("hi"):
        pass

    assert llm_costs(spans) == [0.0123]


async def test_a_free_call_reports_zero_not_nothing(spans):
    await model(cost=0.0).ainvoke("hi")

    assert llm_costs(spans) == [0.0]


async def test_no_reported_cost_leaves_the_span_without_one(spans):
    await model(cost=None).ainvoke("hi")

    assert llm_costs(spans) == [None]


def test_installing_the_hook_twice_wraps_once(spans):
    wrapped = _tracer._update_span

    tracing._report_provider_cost()

    assert _tracer._update_span is wrapped


@pytest.mark.parametrize(
    "outputs, expected",
    [
        (None, None),
        ({}, None),
        ({"llm_output": {"token_usage": {"cost": 0.5}}}, 0.5),
        ({"llm_output": {"token_usage": {"cost": 0}}, "generations": []}, 0.0),
        (
            {
                "llm_output": None,
                "generations": [
                    [
                        {
                            "message": {
                                "kwargs": {"response_metadata": {"token_usage": {"cost": 0.25}}}
                            }
                        }
                    ]
                ],
            },
            0.25,
        ),
        ({"llm_output": {"token_usage": {"cost": "free"}}}, None),
        ({"llm_output": {"token_usage": {"cost": True}}}, None),
        ({"llm_output": {"token_usage": {"cost": -1}}}, None),
        ({"llm_output": {"token_usage": {"cost": float("nan")}}}, None),
    ],
)
def test_reported_cost_reads_only_a_sane_number(outputs, expected):
    assert reported_cost(outputs) == expected
