# HATUA — single-container deployment.
#
# Deliberately boring: one service, no database container, no message broker.
# A six-day build that needs three services to come up in the right order is a
# six-day build that fails during the demo. State is a JSON snapshot on disk,
# and the interface is narrow enough to swap for Postgres/PostGIS later.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hatua ./hatua
COPY scripts ./scripts

# Ship the last snapshot if one was built, so the container serves real
# advisories the instant it starts rather than an empty dashboard while the
# first pipeline run completes.
COPY data ./data

# Writable cache for HTTP responses and snapshots.
RUN mkdir -p /app/.cache /app/data

EXPOSE 8000

# Render and most PaaS providers inject $PORT.
CMD ["sh", "-c", "uvicorn hatua.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
