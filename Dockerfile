FROM node:24-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: build fails rather than drifting from the lockfile. No --extra dev.
RUN uv sync --frozen

FROM python:3.12-slim

# openssh-client: the ssh provider and the break-glass launcher. rsync: `task deploy:sync`
# copies the workspace in and out through `docker exec`.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*

# A real account, since ssh refuses to run for a uid without one. Build with the uid/gid that
# should own the workspace on the host (the deploy overlay passes theseus's).
ARG ARGUS_UID=1000
ARG ARGUS_GID=100
RUN groupadd --gid "$ARGUS_GID" --non-unique argus \
    && useradd --uid "$ARGUS_UID" --gid "$ARGUS_GID" --non-unique --create-home argus \
    && mkdir -p /var/lib/argus/agent-workspace && chown argus: /var/lib/argus/agent-workspace

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic.ini ./
COPY --from=frontend /frontend/dist ./src/argus/api/static
ENV PATH="/app/.venv/bin:$PATH"

# The docker provider reaches the host's socket through its group (compose's group_add).
USER argus

ENV ARGUS_CONFIG=/config/argus.yaml
EXPOSE 8421
# Migrations run on every start; a no-op when the schema is current.
ENTRYPOINT ["sh", "-c", "alembic upgrade head && exec uvicorn argus.api.app:create_app --factory \"$@\"", "argus"]
CMD ["--host", "0.0.0.0", "--port", "8421"]
