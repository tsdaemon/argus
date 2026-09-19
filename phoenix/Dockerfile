# Arize Phoenix with one patch: cost reported on a span (`llm.cost.*`, e.g. OpenRouter's
# `usage.cost`) is used as-is instead of being re-derived from token counts and Phoenix's
# built-in price table, which cannot match OpenRouter model ids like
# `anthropic/claude-sonnet-4.5`. Upstream request, still open when this was written:
# https://github.com/Arize-ai/phoenix/issues/15240
# Drop this directory and use the stock image again once Phoenix ships the behaviour.

# Pinned: the patch applies to this exact file. A different Phoenix version that changed it
# fails the build at the `patch` step below, which is the intended alarm.
ARG PHOENIX_VERSION=version-20.14.0

FROM arizephoenix/phoenix:${PHOENIX_VERSION} AS stock

# The Phoenix image has no shell or patch tool, so apply the patch in a throwaway stage.
FROM alpine:3.22 AS patched
WORKDIR /work
COPY --from=stock /phoenix/.venv/lib/python3.13/site-packages/phoenix/server/daemons/span_cost_calculator.py \
    src/phoenix/server/daemons/span_cost_calculator.py
COPY reported-cost.patch .
RUN apk add --no-cache patch && patch -p1 < reported-cost.patch

FROM stock
COPY --from=patched /work/src/phoenix/server/daemons/span_cost_calculator.py \
    /phoenix/.venv/lib/python3.13/site-packages/phoenix/server/daemons/span_cost_calculator.py
