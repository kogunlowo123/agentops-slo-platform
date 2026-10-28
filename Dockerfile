# syntax=docker/dockerfile:1.7
FROM python:3.15.0-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.15.0-slim-trixie AS runtime
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 agentops \
 && useradd --system --uid 10001 --gid agentops --home-dir /nonexistent --shell /usr/sbin/nologin agentops \
 && mkdir -p /var/lib/agentops/audit \
 && chown -R 10001:10001 /var/lib/agentops
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 policies ./policies
COPY --chown=10001:10001 configs ./configs
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTOPS_HOST=0.0.0.0 \
    AGENTOPS_PORT=8080 \
    AGENTOPS_ACCESS_POLICY_FILE=/app/policies/agentops-access.yaml \
    AGENTOPS_RECOVERY_POLICY_FILE=/app/policies/recovery-actions.yaml
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["python", "-m", "agentops_slo_platform"]
