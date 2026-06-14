FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN grep -Ev '^pytest' requirements.txt > /tmp/requirements-runtime.txt \
    && pip install --no-cache-dir -r /tmp/requirements-runtime.txt

COPY main.py .
COPY rtt_alhuda/ ./rtt_alhuda/
COPY templates/ ./templates/

ENV RTT_ALHUDA_LISTEN_HOST=0.0.0.0
ENV RTT_ALHUDA_LISTEN_PORT=3000

EXPOSE 3000

CMD ["python", "main.py"]
