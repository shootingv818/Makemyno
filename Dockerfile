# Image used for BOTH bots and for every worker: one build, three roles.
# MODE (owner | customer | worker) decides which one starts, so a worker can
# never accidentally be given a bot token it does not have.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps for Pillow/reportlab wheels that may need compiling, plus the
# minimum runtime libs. Removed again in the same layer to keep the image small.
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      gcc libjpeg62-turbo-dev zlib1g-dev libffi-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y gcc libffi-dev && apt-get autoremove -y

# The .git directory is copied deliberately: a worker has no git binary, and
# /ping reads the commit straight out of .git so the owner panel can show which
# workers are running old code.
COPY . .

# Sessions and job state live here; the host bind-mounts it so a container
# rebuild never loses a login.
VOLUME ["/app/data"]

CMD ["python", "main.py"]
