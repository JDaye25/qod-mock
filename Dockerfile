FROM python:3.12-slim

WORKDIR /app

# Optional: keep images quieter/cleaner
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is useful (and required if any deps use git urls)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache-friendly)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /app/requirements.txt

# Copy the repo code into the image
COPY . /app

# Install *this repo* as a package (editable)
# Using python -m pip avoids PATH/venv confusion
RUN python -m pip install -e /app

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
