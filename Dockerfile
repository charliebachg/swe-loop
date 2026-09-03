FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY swe_loop ./swe_loop
RUN uv pip install --system --no-cache .
COPY configs ./configs
COPY templates ./templates
COPY playbooks ./playbooks
COPY knowledge ./knowledge
COPY schemas ./schemas
COPY data ./data
EXPOSE 8000
CMD ["uvicorn", "swe_loop.app:app", "--host", "0.0.0.0", "--port", "8000"]
