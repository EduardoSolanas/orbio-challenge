# Build from the repository root: docker build -f backend_api/Dockerfile .
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data/backend && chown -R appuser:appuser /data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser backend_api ./backend_api
COPY --chown=appuser:appuser shared ./shared
USER appuser
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "backend_api.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
