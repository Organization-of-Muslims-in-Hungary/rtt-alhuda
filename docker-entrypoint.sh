#!/bin/sh
set -e

# Apply database migrations, then start the API server.
alembic upgrade head
exec python -m uvicorn main:app --host "${RTT_ALHUDA_LISTEN_HOST:-0.0.0.0}" --port "${RTT_ALHUDA_LISTEN_PORT:-3000}"
