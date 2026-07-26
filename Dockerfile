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

# Writable dirs for the HTTP cache and the advisory snapshot.
RUN mkdir -p /app/.cache /app/data

# Ship the last snapshot so the container serves real advisories the instant it
# starts, rather than an empty dashboard while the first pipeline run completes.
#
# data/ must exist in the repo for this to work — Docker fails the entire build
# on a missing COPY source, which is what broke the first Render deploy. There
# is a committed data/snapshot.json and a .gitkeep so it always does.
COPY data ./data

EXPOSE 8000

# Render and most PaaS providers inject $PORT.
CMD ["sh", "-c", "uvicorn hatua.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
