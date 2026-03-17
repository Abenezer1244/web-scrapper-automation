FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright + psycopg2
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only)
RUN playwright install chromium && playwright install-deps chromium

COPY . .

# Create non-root user
RUN useradd -m -u 1000 bridge && chown -R bridge:bridge /app
USER bridge

EXPOSE 8000
