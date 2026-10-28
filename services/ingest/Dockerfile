FROM python:3.12-slim

# Non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

USER appuser

CMD ["python", "-m", "app"]
