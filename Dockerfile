FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install required system packages
# NOTE: no apt `nodejs` — Debian bookworm ships Node 18, but the
# bgutil PO Token provider server (below) requires Node >= 22, so a
# pinned official Node 22 tarball is installed instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    xz-utils \
    ca-certificates \
    fontconfig \
    fonts-sil-padauk \
    fonts-noto-extra \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS (pinned + checksum-verified) — runtime for the
# bgutil PO Token provider server. Render builds on linux-x64.
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
RUN curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt gradio_client

# Copy application code
COPY . .

# Register authentic Myanmar Unicode fonts with system fontconfig
RUN mkdir -p /usr/share/fonts/truetype/myanmar /etc/fonts/conf.d \
    && cp app/assets/fonts/*.ttf /usr/share/fonts/truetype/myanmar/ \
    && cp app/assets/fonts/99-myanmar.conf /etc/fonts/conf.d/ \
    && chmod 644 /usr/share/fonts/truetype/myanmar/*.ttf \
    && fc-cache -f -v

# Create necessary directories
RUN mkdir -p data storage/models storage/temp

# bgutil PO Token provider server (pinned release, checksum-verified).
# Fetched and compiled at image-build time only — no network fetch at
# container runtime. The yt-dlp plugin half is the pip package
# bgutil-ytdlp-pot-provider in requirements.txt; this is the server
# half. It listens on loopback only (docker-entrypoint.sh) and hands
# proof-of-origin tokens to yt-dlp so YouTube's web clients pass the
# datacenter bot check. Build mirrors the upstream server Dockerfile:
# npm ci (full, for tsc) -> npx tsc -> prune dev deps.
ARG BGUTIL_VERSION=2.0.2
ARG BGUTIL_SHA256=1f2ae9ec39405b8e80397dfc2551f58deed09d8dff4b2b8fbc314df8e091c0a1
RUN curl -fsSL -o /tmp/bgutil.tar.gz "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${BGUTIL_VERSION}.tar.gz" \
    && echo "${BGUTIL_SHA256}  /tmp/bgutil.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/bgutil-pot-server \
    && tar -xzf /tmp/bgutil.tar.gz -C /opt/bgutil-pot-server --strip-components=2 "bgutil-ytdlp-pot-provider-${BGUTIL_VERSION}/server" \
    && rm /tmp/bgutil.tar.gz \
    && cd /opt/bgutil-pot-server \
    && npm ci --no-audit --no-fund \
    && npx tsc \
    && npm prune --omit=dev --no-audit --no-fund \
    && test -f build/main.js

# Cloudflare WARP egress sidecar client — usque (pinned release,
# checksum-verified). YouTube walls Render's own egress IP (R39/
# R39.1 + POT proofs), while WARP's free egress passes (feasibility
# proof with the app's own extraction code, 2026-11-02). usque runs
# in MASQUE-over-HTTP/2 mode (TCP — Render restricts UDP egress) as
# a loopback SOCKS5 proxy started by docker-entrypoint.sh; only
# YouTube attempts route through it (app/services/downloader.py).
# Fetched at image-build time only. The anonymous device config is
# registered at container start and kept on the persistent disk —
# NEVER baked into the image (this repo is public, and the config
# holds the device private key).
ARG USQUE_VERSION=4.2.1
ARG USQUE_SHA256=4117e20695078af9c11edecd1a826c009bbc7ea0b7f64458612b4198910bc313
RUN curl -fsSL -o /tmp/usque.zip "https://github.com/Diniboy1123/usque/releases/download/v${USQUE_VERSION}/usque_${USQUE_VERSION}_linux_amd64.zip" \
    && echo "${USQUE_SHA256}  /tmp/usque.zip" | sha256sum -c - \
    && mkdir -p /opt/usque \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/usque.zip').extract('usque', '/opt/usque')" \
    && chmod +x /opt/usque/usque \
    && rm /tmp/usque.zip \
    && /opt/usque/usque version

# Run Voice Clone Pro Official Bot & Worker.
# The entrypoint starts the POT provider server first (bounded wait,
# app still starts if it fails), then the WARP egress sidecar
# (registers a fresh anonymous device on first boot if the
# persistent disk has no config; bounded wait, app still starts if
# it fails), then execs this CMD. /health behaviour is unchanged —
# it is served by the app as before.
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py", "--mode", "all"]
