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

# Run Voice Clone Pro Official Bot & Worker.
# The entrypoint starts the POT provider server first (bounded wait,
# app still starts if it fails), then execs this CMD. /health
# behaviour is unchanged — it is served by the app as before.
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py", "--mode", "all"]
