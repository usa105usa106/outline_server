FROM python:3.11-slim

ARG OUTLINE_SS_VERSION=1.9.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar nginx \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) oss_arch="x86_64" ;; \
      arm64) oss_arch="arm64" ;; \
      armhf) oss_arch="armv7" ;; \
      *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac; \
    mkdir -p /tmp/oss; \
    curl -fsSL -o /tmp/outline-ss-server.tgz \
      "https://github.com/OutlineFoundation/tunnel-server/releases/download/v${OUTLINE_SS_VERSION}/outline-ss-server_${OUTLINE_SS_VERSION}_linux_${oss_arch}.tar.gz"; \
    tar -xzf /tmp/outline-ss-server.tgz -C /tmp/oss; \
    find /tmp/oss -type f -name outline-ss-server -exec cp {} /usr/local/bin/outline-ss-server \;; \
    chmod +x /usr/local/bin/outline-ss-server; \
    outline-ss-server -version || true; \
    rm -rf /tmp/oss /tmp/outline-ss-server.tgz

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

CMD ["python", "bot.py"]