FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY rtt_alhuda ./rtt_alhuda
COPY templates ./templates

# Match fly.toml internal_port (8080). Override with RTT_ALHUDA_LISTEN_PORT / PORT at runtime.
ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
