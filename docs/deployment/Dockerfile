# Template: application code and a pinned requirements.txt do not exist yet.
# One image ships both Python packages. The gateway runs app.main:app; the private
# Order API service overrides the command with order_api.main:app on port 8001.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data/sessions && chown -R appuser:appuser /data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser order_api ./order_api
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "40"]
