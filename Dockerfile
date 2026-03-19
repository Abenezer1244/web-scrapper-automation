FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright + psycopg2
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libx11-6 \
    libxext6 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user BEFORE installing Playwright
# so browsers are installed in the correct home directory
RUN useradd -m -u 1000 bridge && chown -R bridge:bridge /app
USER bridge

# Install Playwright Chromium as the bridge user
RUN playwright install chromium

COPY --chown=bridge:bridge . .
RUN chmod +x /app/start.sh

EXPOSE 8000

CMD ["/app/start.sh"]
