FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project

COPY . .
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MCP_HOST=0.0.0.0

EXPOSE 8000 8501 8001 8002 8003
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
