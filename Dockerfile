FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.11-slim-bookworm AS build
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY collector/pyproject.toml collector/uv.lock ./collector/
COPY docker/export_requirements.py ./docker/export_requirements.py
# CPU wheels keep the default image usable on ordinary Docker hosts, without CUDA.
RUN python docker/export_requirements.py /app /tmp/kb-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python --torch-backend cpu -r /tmp/kb-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --directory /app/collector --frozen --no-dev --no-install-project
COPY kb ./kb
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps --editable .

FROM python:3.11-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 kb && useradd --uid 1000 --gid kb --create-home kb
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/collector/.venv /app/collector/.venv
COPY pyproject.toml uv.lock ./
COPY kb ./kb
COPY scripts ./scripts
COPY collector ./collector
COPY docker/entrypoint.py ./docker/entrypoint.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_SYNC=1 UV_PYTHON_DOWNLOADS=never \
    KB_CONTAINER=1 KB_HOST=0.0.0.0 PORT=8787 \
    KB_DATA_DIR=/app/data KB_VAULT_ROOT=/vault/new-start \
    KB_COLLECTOR_DIR=/app/collector HF_HOME=/home/kb/.cache/huggingface
RUN mkdir -p /app/data /app/collector/data /vault/new-start /home/kb/.cache/huggingface \
    && chown -R kb:kb /app/data /app/collector/data /vault /home/kb
USER kb
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["kb-web"]
