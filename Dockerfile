# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv build --wheel && uv pip install --no-deps dist/*.whl

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 commerce \
    && useradd --system --uid 10001 --gid 10001 --create-home commerce
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8783
ENTRYPOINT ["commerce"]
CMD ["api", "--host", "0.0.0.0", "--port", "8783"]
