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
COPY scripts/ scripts/

# Copy default manifest (can be overridden via volume mount)
# COPY manifest.json .

# Copy test directory with filters for test mode
# COPY test/ test/

# Install dependencies using uv
# RUN uv pip install --system -r requirements.txt
RUN pip install -r requirements.txt

# Expose the internal mTLS port. docker-compose publishes nginx, not this port.
EXPOSE 8443

# Environment variables
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; response = requests.get('https://localhost:8443/health', cert=('/app/tls/internal/nginx-client.crt', '/app/tls/internal/nginx-client.key'), verify='/app/tls/internal/ca.crt'); response.raise_for_status()" || exit 1

# Run the API with internal mTLS. nginx verifies this server certificate and
# Uvicorn requires nginx to present a client certificate signed by the same CA.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8443", "--ssl-certfile", "/app/tls/internal/hushfilter-api.crt", "--ssl-keyfile", "/app/tls/internal/hushfilter-api.key", "--ssl-ca-certs", "/app/tls/internal/ca.crt", "--ssl-cert-reqs", "2"]
