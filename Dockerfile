# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/app.db \
    BACKUP_DIR=/data/backups

WORKDIR /app

# Install deps first for layer caching
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
      "fastapi>=0.110" "uvicorn[standard]>=0.29" "sqlalchemy>=2.0" "alembic>=1.13" \
      "ortools>=9.9" "itsdangerous>=2.1" "jinja2>=3.1" "python-multipart>=0.0.9"

COPY . .

# Non-root user; data dir is a mounted volume
RUN useradd -u 10001 -m appuser && mkdir -p /data && chown -R appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
