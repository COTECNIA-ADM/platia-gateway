FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 app
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN chown -R app:app /app
USER app

EXPOSE 8000
# Con PROMETHEUS_MULTIPROC_DIR (G12), los procesos de uvicorn comparten las métricas en ese
# directorio: se vacía al arrancar para no sumar las de una ejecución anterior
CMD ["sh", "-c", "if [ -n \"$PROMETHEUS_MULTIPROC_DIR\" ]; then rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\"; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --no-server-header"]
