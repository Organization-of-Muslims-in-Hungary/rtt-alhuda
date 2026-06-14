FROM python:3.12-slim-bookworm AS builder

# webrtcvad has no pre-built wheel for linux/arm64 + Python 3.12 — compile in builder.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN grep -Ev '^pytest' requirements.txt > /tmp/requirements-runtime.txt \
    && pip install --no-cache-dir --prefix=/install -r /tmp/requirements-runtime.txt

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY main.py .
COPY rtt_alhuda/ ./rtt_alhuda/
COPY templates/ ./templates/

ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=3000

EXPOSE 3000

CMD ["python", "main.py"]
