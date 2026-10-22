FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    ca-certificates \
    nodejs \
    fontconfig \
    fonts-sil-padauk \
    fonts-noto-extra \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

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

# Run Voice Clone Pro Official Bot & Worker
CMD ["python", "main.py", "--mode", "all"]
