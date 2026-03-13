FROM python:3.13-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire project
COPY . .

# Cloud Run sets PORT env var (default 8080)
ENV PORT=8080
EXPOSE 8080

# Start the server — Cloud Run requires 0.0.0.0 binding
CMD ["sh", "-c", "uvicorn run_combined:app --host 0.0.0.0 --port $PORT"]
