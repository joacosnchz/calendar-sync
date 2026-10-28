# Dev/test only: the mock Microsoft identity platform + Graph, and the e2e smoke test.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --group dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group dev \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app /data
USER app

EXPOSE 8001
CMD ["uvicorn", "mockgraph.main:app", "--host", "0.0.0.0", "--port", "8001"]
