# Dockerfile - builds the QoD API image
FROM python:3.12-slim

WORKDIR /app

# Your requirements install a git+https dependency, so we need git inside the image
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install -r /app/requirements.txt

# Copy the rest of the repo
COPY . /app

EXPOSE 8000

# Run the API (no --reload in "prod")
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]