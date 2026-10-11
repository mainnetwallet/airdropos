# ==============================================================================
# AirdropOS Backend & Worker Dockerfile
# ==============================================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install system dependencies including those needed for Playwright and Web3
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r backend/requirements.txt

# Install Playwright browsers (chromium)
RUN playwright install --with-deps chromium

# Copy application source code
COPY backend/ /app/backend/

# Target: Backend API Server
FROM base AS backend
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Target: Background 24/7 Worker Daemon
FROM base AS worker
CMD ["python", "-m", "backend.app.scheduler.worker"]
