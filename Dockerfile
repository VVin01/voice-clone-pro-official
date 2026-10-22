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
    fonts-sil-padauk \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt gradio_client

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p data storage/models storage/temp

# Run Voice Clone Pro Official Bot & Worker
CMD ["python", "main.py", "--mode", "all"]
