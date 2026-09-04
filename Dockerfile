FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# pinned: :latest would make two builds of the same commit different images
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY swe_loop ./swe_loop
RUN uv pip install --system --no-cache .

COPY configs ./configs
COPY templates ./templates
COPY static ./static
COPY playbooks ./playbooks
COPY knowledge ./knowledge
COPY schemas ./schemas
COPY data ./data

# nothing here needs root, and the mounted clone is read with the same account
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "swe_loop.app:app", "--host", "0.0.0.0", "--port", "8000"]
