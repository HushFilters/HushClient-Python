# Dockerfile for HushFilter API
FROM python:3.11-slim

# Install uv
# COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Set working directory
WORKDIR /app

# Copy project files
COPY requirements.txt .
COPY *.py .

# Copy core modules
COPY core/ core/
COPY helpers/ helpers/
COPY webui/ webui/
COPY filter_sync/ filter_sync/

# Copy default manifest (can be overridden via volume mount)
# COPY manifest.json .

# Copy test directory with filters for test mode
# COPY test/ test/

# Install dependencies using uv
# RUN uv pip install --system -r requirements.txt
RUN pip install -r requirements.txt

# Expose port
EXPOSE 8000

# Environment variables
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; response = requests.get('http://localhost:8000/health'); response.raise_for_status()" || exit 1

# Run the API
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
