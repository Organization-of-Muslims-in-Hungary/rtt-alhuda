# Fly: run `fly deploy` from the `rtt-alhuda/` directory (build context = this folder).
#
# Monorepo local build from **repo root** (`khutba-app/`):
#   docker build -f rtt-alhuda/Dockerfile --build-arg APP_PREFIX=rtt-alhuda -t rtt-alhuda .
#
# Pick the final image with `fly.toml` → `[build] build-target`:
#   - runtime-git   — shallow `git clone` each build (latest on FRONTEND_GIT_REF). No frontend
#                     repo in the Docker *context*; the image still COPYs the built `dist/`
#                     from the `frontend-git` stage into the Python runtime (not optional).
#   - runtime-copy  — build Vite from `FRONTEND_SRC` in context (submodule). No clone;
#                     faster/offline-friendly when the submodule is already checked out.
#
# Local from `rtt-alhuda/` (default runtime-copy):
#   docker build -f Dockerfile -t rtt-alhuda .
#
# Local clone-based (same as Fly runtime-git):
#   docker build -f Dockerfile --target runtime-git \
#     --build-arg FRONTEND_GIT_URL=https://github.com/ORG/khutba-app-frontend.git -t rtt-alhuda .
#
# APP_PREFIX: `.` when context is `rtt-alhuda/`; `rtt-alhuda` when context is monorepo root.
#
# ── Stage: Vite app from Git ───────────────────────────────────────────────
FROM node:22-bookworm-slim AS frontend-git
WORKDIR /build
ARG FRONTEND_GIT_URL
ARG FRONTEND_GIT_REF=main
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && if [ -z "${FRONTEND_GIT_URL}" ]; then echo "ERROR: FRONTEND_GIT_URL is required for --target runtime-git" >&2; exit 1; fi \
    && git clone --depth 1 --branch "${FRONTEND_GIT_REF}" "${FRONTEND_GIT_URL}" .
ARG VITE_BACKEND_ORIGIN=
ENV VITE_BACKEND_ORIGIN=$VITE_BACKEND_ORIGIN
ARG VITE_AUDIO_SOURCE=browser
ENV VITE_AUDIO_SOURCE=$VITE_AUDIO_SOURCE
RUN npm ci && npm run build

# ── Stage: Vite app from build context ───────────────────────────────────────
FROM node:22-bookworm-slim AS frontend-copy
WORKDIR /build
ARG FRONTEND_SRC=Khutba-app-frontend
COPY ${FRONTEND_SRC}/package.json ${FRONTEND_SRC}/package-lock.json ./
RUN npm ci
COPY ${FRONTEND_SRC}/ ./
ARG VITE_BACKEND_ORIGIN=
ENV VITE_BACKEND_ORIGIN=$VITE_BACKEND_ORIGIN
ARG VITE_AUDIO_SOURCE=browser
ENV VITE_AUDIO_SOURCE=$VITE_AUDIO_SOURCE
RUN npm run build

# ── Python base (shared by both runtime-* stages) ───────────────────────────
FROM python:3.12-slim-bookworm AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG APP_PREFIX=.
COPY ${APP_PREFIX}/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ${APP_PREFIX}/main.py .
COPY ${APP_PREFIX}/rtt_alhuda ./rtt_alhuda
COPY ${APP_PREFIX}/templates ./templates

# ── Final: clone-based frontend (Fly “always latest” on branch) ─────────────
FROM runtime-base AS runtime-git
COPY --from=frontend-git /build/dist ./frontend_dist
ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=8080
EXPOSE 8080
CMD ["python", "main.py"]

# ── Final: COPY-based frontend (local / submodule in context) ────────────────
FROM runtime-base AS runtime-copy
COPY --from=frontend-copy /build/dist ./frontend_dist
ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=8080
EXPOSE 8080
CMD ["python", "main.py"]
