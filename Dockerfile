# Use official lightweight Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MTGSPOT_DB_PATH=/data/mtgspot.db

# Set working directory
WORKDIR /app

# Install system dependencies (curl for healthcheck, sqlite3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY mtgspot/ /app/mtgspot/
COPY cli.py /app/cli.py

# Create directory for persistent SQLite database
RUN mkdir -p /data

# Expose API and Web UI port
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/stats || exit 1

# Default command: start FastAPI server
CMD ["uvicorn", "mtgspot.api:app", "--host", "0.0.0.0", "--port", "8000"]
