# Use Python 3.10 slim image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements-gateway.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements-gateway.txt

# Copy gateway code
COPY llm_gateway/ ./llm_gateway/

# Copy environment file
COPY INAGENT/.env ./.env

# Expose port
EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:9000/health')"

# Run gateway
CMD ["python", "-m", "uvicorn", "llm_gateway.gateway:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
