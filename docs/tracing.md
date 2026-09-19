# Tracing

argus sends traces of every agent run to [Arize Phoenix](https://github.com/Arize-ai/phoenix)
using OpenTelemetry and the OpenInference conventions. A trace shows each graph step, model
call, and tool call, with token counts and, for OpenRouter models, the cost.

Tracing is optional and must never affect a run. It is off unless `agent.otel.enabled` is true,
and any setup failure is logged and ignored.

## Turning it on

Local development already has it on. `examples/argus.dev.yaml`:

```yaml
agent:
  otel:
    enabled: true
    endpoint: http://127.0.0.1:6006/v1/traces
    project_name: argus-agent   # default
```

- `task deps:up` (or `task dev`) starts Phoenix. Its UI is at http://127.0.0.1:6006.
- The endpoint's scheme picks the protocol. `http://host:6006/v1/traces` is OTLP over HTTP,
  which is the port Compose publishes. `http://host:4317` is gRPC, which the Compose network
  reaches as `http://phoenix:4317` (used by `examples/argus.example.yaml`); 4317 is not
  published to the host.
- Tracing is set up once, when the app is created. Under `task dev` the agent restarts by
  itself when `examples/argus.dev.yaml` changes, so a change to `agent.otel` takes effect on its
  own; otherwise restart it (in mprocs, select `agent` and press `r`).
- Traces appear under the `argus-agent` project.

## What is captured

`argus.agent.tracing.setup_tracing` registers a Phoenix tracer provider (batched, exported from
a background thread) and instruments LangChain/LangGraph with the OpenInference LangChain
instrumentor. That covers the LangGraph run, each model call, and each tool call.

**Sessions.** The instrumentor reads `thread_id` from the run metadata and sets `session.id` on
every span, so one argus conversation is one Phoenix session. This was verified with a plain
LangGraph and the same `thread_id` mechanism; it has not been watched with a full argus chat.

## Cost

Phoenix shows a cost for a span, and per trace and session. The pipeline for an OpenRouter
call is:

```
OpenRouter response  usage.cost
  -> CostReportingChatOpenAI   keeps it on streamed messages     (argus.agent.model)
  -> tracing hook              sets llm.cost.total on the span   (argus.agent.tracing)
  -> patched Phoenix           stores it as the span's cost      (phoenix/)
```

Each step exists because something upstream does not do it:

| Gap | Where | Workaround |
|---|---|---|
| OpenRouter returns the real charge as `usage.cost` on every response, but `langchain-openai` drops it when it converts a *streamed* usage chunk (it keeps it for non-streaming calls). The agent streams. | `langchain-openai` | `CostReportingChatOpenAI` in `src/argus/agent/model.py` puts it back on the message's `response_metadata.token_usage`. |
| The OpenInference LangChain instrumentor (0.1.76) records token counts but has no cost handling. | `openinference-instrumentation-langchain` | `_report_provider_cost()` in `src/argus/agent/tracing.py` wraps the instrumentor's private `_update_span` and sets `llm.cost.total` when `reported_cost()` finds a cost. |
| Phoenix ignores `llm.cost.*` on a span. It computes cost only from token counts and its own price table, matched by regex on the model name, which does not match OpenRouter ids such as `anthropic/claude-sonnet-4.5`. | Phoenix, [issue #15240](https://github.com/Arize-ai/phoenix/issues/15240) | The patched image in `phoenix/`. |

Arize's OpenAI instrumentor does not handle cost either, so switching instrumentors would not
help.

### The patched Phoenix image

`phoenix/Dockerfile` builds stock `arizephoenix/phoenix:version-20.14.0` with one file replaced,
using `phoenix/reported-cost.patch` (a unified diff with `src/phoenix/...` paths, so it also
applies to the upstream repo). Compose builds the `phoenix` service from it as
`argus-phoenix:local`, and `task deps:up` passes `--build`. The behaviour:

- `llm.cost.prompt` / `llm.cost.completion`, when reported, replace the computed cost of that
  side.
- A lone `llm.cost.total` (all OpenRouter reports) is spread over the token types, weighted by
  the computed cost when a model matched and by token counts otherwise. **The total is exact;
  the prompt/completion split is an estimate.**
- With no reported cost, the price-table result is unchanged.

The Dockerfile applies the patch in an Alpine stage (the Phoenix image has no shell) and fails
the build if the patched file changed, which is the signal to redo the patch on a new version.

### Conventions

OpenTelemetry's GenAI conventions have no cost attribute yet; `gen_ai.usage.cost.*` is proposed
(`open-telemetry/semantic-conventions-genai`, issues #443 and #484, open when checked).
OpenInference defines `llm.cost.prompt`, `llm.cost.completion`, `llm.cost.total` and detail
keys, in USD, and that is what Phoenix consumes.

## Checking that it works

1. Restart the agent, send a message, and open the `argus-agent` project in Phoenix.
2. LLM spans should show token counts and a cost. A new row appears under **Sessions**, and its
   id is the `thread` in the page URL.
3. To test the cost path without a model key, send a span by hand:

   ```python
   from phoenix.otel import register
   provider = register(endpoint="http://127.0.0.1:6006/v1/traces", project_name="cost-check",
                       batch=False, verbose=False)
   with provider.get_tracer("t").start_as_current_span("call") as span:
       span.set_attributes({"openinference.span.kind": "LLM", "llm.model_name": "x/y",
                            "llm.token_count.prompt": 1000, "llm.token_count.completion": 200,
                            "llm.token_count.total": 1200, "llm.cost.total": 0.0123})
   provider.force_flush()
   ```

   Phoenix computes cost every five seconds, so wait a moment. Delete the project afterwards.

`tests/agent/test_reported_cost.py` covers the argus half without a network (a fake gateway over
`httpx.MockTransport`), and `tests/agent/test_tracing.py` covers enable/disable and failure.

## Troubleshooting

- **Nothing in Phoenix.** Was the agent restarted after enabling tracing? Is
  `docker ps` showing `argus-phoenix-1` with port `127.0.0.1:6006`? If the port shows no
  mapping, restart the container: `docker compose --profile test restart phoenix`.
- **Spans but no cost.** Cost appears only for calls that report it, which OpenRouter does.
  Confirm Phoenix is running the patched image (`docker ps` shows `argus-phoenix:local`, not
  `arizephoenix/phoenix`).
- **Phoenix data disappears.** The container has no volume, so recreating it clears traces.
- **A warning "Tracing setup failed".** Read the log line; the agent still runs without traces.

## Upgrading

- **Phoenix:** change `PHOENIX_VERSION` in `phoenix/Dockerfile`. If the build fails at `patch`,
  the file changed: re-apply the change by hand and regenerate the patch. Drop the whole
  `phoenix/` directory (use the stock image) once #15240 is fixed upstream.
- **`openinference-instrumentation-langchain`:** the cost hook depends on the private
  `_tracer._update_span`. The tests fail if it moves. Remove the hook once the instrumentor
  reports cost itself.
- **`langchain-openai`:** remove `CostReportingChatOpenAI` if it starts keeping `usage.cost` on
  streamed messages (the streaming test in `test_reported_cost.py` shows when it does).

## Not yet verified

- A real OpenRouter call: the response shape used in the tests comes from OpenRouter's
  usage-accounting documentation and a stub.
- A full agent run through the UI, including that the Sessions grouping matches the thread.
