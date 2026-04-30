FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SS_PORT=8388 \
    SS_METHOD=chacha20-ietf-poly1305 \
    SS_TIMEOUT=300 \
    STATE_PATH=/data/outline_state.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends shadowsocks-libev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py ./

EXPOSE 8388/tcp

CMD ["python", "bot.py"]
