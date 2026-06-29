FROM python:3.12-slim-bookworm AS builder


RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN grep -Ev '^pytest' requirements.txt > /tmp/requirements-runtime.txt \
    && pip install --no-cache-dir --prefix=/install -r /tmp/requirements-runtime.txt

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 libpulse0 libasound2-plugins \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY main.py .
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY rtt_alhuda/ ./rtt_alhuda/
COPY asound.conf /etc/asound.conf
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN mkdir -p /data

ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=3000
ENV PYTHONUNBUFFERED=1
ENV KHUTBA_DB_PATH=/data/alhuda.db

EXPOSE 3000

CMD ["sh", "/app/docker-entrypoint.sh"]
