# AnyDown production image: Python + FFmpeg + Node 22 + bgutil POT provider.
FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YTDLP_POT_PROVIDER_URL=http://127.0.0.1:4416

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

# Build the current PO-token provider. Version 2.0.0 includes important server
# security fixes; keep the plugin and provider versions aligned.
RUN git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci --no-audit --no-fund \
    && npx tsc

COPY . .
RUN mkdir -p /app/downloads && chmod +x /app/start.sh

EXPOSE 10000
CMD ["/app/start.sh"]
