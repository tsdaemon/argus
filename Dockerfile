FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: build fails rather than silently drifting from the committed lockfile.
# No --extra dev: test/lint tooling has no business in the runtime image.
RUN uv sync --frozen

FROM python:3.12-slim

# openssh-client: only needed if the breakglass `launcher: {type: ssh}` is configured
# (see README "Session launch") — harmless to always include.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH"

# Runs as root by default: the docker provider needs to reach a socket mounted in
# from the host, and container root is not host root. If you don't need the docker
# provider, drop the socket mount and run as a non-root user instead.

EXPOSE 8420
ENTRYPOINT ["argus"]
CMD ["serve", "--config", "/config/argus.yaml", "--host", "0.0.0.0", "--port", "8420"]
