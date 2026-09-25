FROM python:3.12-slim

# linux/amd64 ve linux/arm64 (Apple Silicon / M2 Ultra) destekler
LABEL org.opencontainers.image.title="LLMPerf" \
      org.opencontainers.image.description="Local Ollama performance monitor"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLMPERF_OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    LLMPERF_HOST=0.0.0.0 \
    LLMPERF_PORT=8080

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin llmperf

COPY backend /app/backend
COPY frontend /app/frontend

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
