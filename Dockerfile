FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source only — no fixtures, caches, or DB files
COPY src/ ./src/

# Seed data committed to the repo.  restore_if_empty() loads this on first
# boot when /data/metacrawl.db is empty, so the service is immediately useful
# after deploy without running any crawls.
RUN mkdir -p /app/seeds
COPY seeds/ ./seeds/

# Persistent disk mounts at /data on Render; DB lives there.
# The directory is created at build time so it exists even without the disk.
RUN mkdir -p /data

# HF Spaces runs as uid 1000 (non-root).  /data must be world-writable so
# the app can create the SQLite file there.  /app/seeds is included so the
# seeds directory is also writable if needed.
RUN mkdir -p /data /app/seeds && chmod -R 777 /data
ENV HOME=/tmp

ENV PYTHONPATH=/app/src

# Redirect the HTTP disk cache to /tmp so uid 1000 can write it.
# Default is ".cache/http" (relative to /app CWD), which is root-owned.
ENV MC_CACHE_DIR=/tmp/.cache/http

# Default DB inside /data (world-writable).  Overridden by render.yaml and
# HF Spaces env vars if needed; kept here so the container works out of the
# box without any env configuration.
ENV MC_DB_URL=sqlite:////data/metacrawl.db

EXPOSE 8000

CMD ["python", "-m", "metacrawl.web"]
