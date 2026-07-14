FROM python:3.12-slim

# Install curl, gnupg and google-chrome-stable with all dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    && curl -fSsL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor | tee /usr/share/keyrings/google-chrome.gpg > /dev/null \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for fast dependency synchronization
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy python project configuration
COPY pyproject.toml uv.lock ./

# Create a virtual environment and install dependencies using uv
RUN uv venv && uv sync --frozen

# Install Playwright Chromium and its Linux system dependencies (fonts, libraries)
RUN .venv/bin/playwright install --with-deps chromium

# Copy API service code
COPY service ./service

EXPOSE 11235

# Start uvicorn server pointing to the crawl4ai wrapper app
CMD [".venv/bin/uvicorn", "service.crawl4ai_api:app", "--host", "0.0.0.0", "--port", "11235"]
