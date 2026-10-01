from __future__ import annotations

import json

import httpx
import pytest

from argus.classifier import AskClassifier, CachingClassifier, build_classifier
from argus.classifier.jev import JevClassifier
from argus.classifier.llm import LlmClassifier
from argus.policy import CallClassification, ToolClass


def jev(read: float, mutate: float, destroy: float, requests: list | None = None) -> JevClassifier:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        probabilities = {"read": read, "mutate": mutate, "destroy": destroy}
        choice = max(probabilities, key=probabilities.get)
        return httpx.Response(
            200,
            json={"answers": {"risk": {"type": "choice", "choice": choice, "probabilities": probabilities}}},
        )

    return JevClassifier(
        api_key="sk-test", context="test router",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# --- jev ---


@pytest.mark.asyncio
async def test_jev_confident_read_is_read():
    assert (await jev(1, 0, 0).classify("nvram get wan0_dns")).tool_class is ToolClass.READ


@pytest.mark.asyncio
async def test_jev_confident_destroy_is_destructive():
    assert (await jev(0, 0.04, 0.96).classify("mtd-erase2 nvram")).tool_class is ToolClass.DESTRUCTIVE


@pytest.mark.asyncio
async def test_jev_change_is_mutate():
    assert (await jev(0, 1, 0).classify("service restart_wan")).tool_class is ToolClass.MUTATE


@pytest.mark.asyncio
async def test_jev_unsure_answer_falls_to_mutate_even_when_destroy_leads():
    # The disguised `nvram erase` from the trial: destroy leads but is under the threshold.
    result = await jev(0.33, 0.06, 0.61).classify('sh -c "$(echo bnZyYW0gZXJhc2U= | base64 -d)"')

    assert result.tool_class is ToolClass.MUTATE
    assert "read 0.33" in result.note and "destroy 0.61" in result.note


@pytest.mark.asyncio
async def test_jev_sends_the_command_as_state_with_the_key():
    requests: list[httpx.Request] = []
    await jev(1, 0, 0, requests).classify("ip route")

    (request,) = requests
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == {"host": "test router", "command": "ip route"}
    assert set(body["questions"]["risk"]["criteria"]) == {"read", "mutate", "destroy"}


@pytest.mark.asyncio
async def test_jev_http_error_raises():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    with pytest.raises(httpx.HTTPStatusError):
        await JevClassifier(api_key="k", context="c", client=client).classify("ip route")


# --- llm ---


class FakeStructuredModel:
    def __init__(self, verdict) -> None:
        self.verdict = verdict
        self.prompts: list = []
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        return self

    async def ainvoke(self, messages):
        self.prompts.append(messages)
        return self.schema(**self.verdict)


@pytest.mark.parametrize(
    ("risk", "tool_class"),
    [("read", ToolClass.READ), ("mutate", ToolClass.MUTATE), ("destroy", ToolClass.DESTRUCTIVE)],
)
@pytest.mark.asyncio
async def test_llm_maps_its_answer_to_a_class(risk, tool_class):
    model = FakeStructuredModel({"risk": risk, "reason": "because"})
    classifier = LlmClassifier(api_key="k", context="test router", model="m/x", chat_model=model)

    result = await classifier.classify("ip route")

    assert result.tool_class is tool_class
    assert result.note == f"m/x: {risk} (because)"
    ((_, system_text), (_, human_text)) = model.prompts[0]
    assert "test router" in system_text and "not instructions" in system_text
    assert human_text.endswith("ip route")


# --- caching and fail-closed ---


class Counting:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def classify(self, command: str) -> CallClassification:
        self.calls.append(command)
        if self.fail:
            raise RuntimeError("down")
        return CallClassification(ToolClass.READ, "ok")


@pytest.mark.asyncio
async def test_answers_are_cached_by_command():
    inner = Counting()
    classifier = CachingClassifier(inner)

    await classifier.classify("ip route")
    await classifier.classify("ip route")
    await classifier.classify("ip addr")

    assert inner.calls == ["ip route", "ip addr"]


@pytest.mark.asyncio
async def test_failure_means_ask_and_is_not_cached():
    inner = Counting(fail=True)
    classifier = CachingClassifier(inner)

    first = await classifier.classify("ip route")
    await classifier.classify("ip route")

    assert first.tool_class is ToolClass.MUTATE
    assert "unavailable (RuntimeError: down)" in first.note
    assert len(inner.calls) == 4  # each lookup tries twice


@pytest.mark.asyncio
async def test_one_failure_is_retried_and_an_empty_error_is_named():
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def classify(self, command: str) -> CallClassification:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            return CallClassification(ToolClass.READ, "ok")

    assert (await CachingClassifier(Flaky()).classify("ip route")).tool_class is ToolClass.READ

    class Silent:
        async def classify(self, command: str) -> CallClassification:
            raise TimeoutError()

    result = await CachingClassifier(Silent()).classify("ip route")
    assert result.note == "Risk classifier unavailable (TimeoutError)."


# --- factory ---


@pytest.mark.asyncio
async def test_no_classifier_config_means_ask():
    classifier = build_classifier(None, context="c")

    assert isinstance(classifier, AskClassifier)
    assert (await classifier.classify("ip route")).tool_class is ToolClass.MUTATE


def test_build_jev_and_llm_wrapped_in_the_cache():
    jev_classifier = build_classifier({"type": "jev", "api_key": "k", "read_threshold": 0.95}, context="c")
    llm_classifier = build_classifier({"type": "llm", "api_key": "k", "model": "m/x"}, context="c")

    assert isinstance(jev_classifier, CachingClassifier)
    assert isinstance(jev_classifier._inner, JevClassifier)
    assert jev_classifier._inner._read_threshold == 0.95
    assert isinstance(llm_classifier._inner, LlmClassifier)


def test_unknown_classifier_type_is_an_error():
    with pytest.raises(ValueError, match="Unknown classifier type"):
        build_classifier({"type": "vibes"}, context="c")
