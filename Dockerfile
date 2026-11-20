FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS base
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM base AS runtime
COPY --from=builder /app/.venv /app/.venv
COPY deployment-lock.json ./
RUN groupadd --gid 1000 pozor && useradd --uid 1000 --gid pozor --create-home pozor \
    && mkdir /data && chown pozor:pozor /data
ENV POZOR_HOME=/app POZOR_DATA_DIR=/data DATABASE_PATH=/data/pozor.sqlite3 ADMIN_HOST=0.0.0.0
USER pozor
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)"
ENTRYPOINT ["pozor"]
CMD ["serve"]
