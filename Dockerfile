FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source only — no fixtures, caches, or DB files
COPY src/ ./src/

# Seed data committed to the repo.  restore_if_empty() loads this on first
# boot when /data/metacrawl.db is empty, so the service starts with data.
RUN mkdir -p /app/seeds
COPY seeds/ ./seeds/

# Persistent disk mounts at /data on Render; DB lives there.
# The directory is created at build time so it exists even without the disk.
RUN mkdir -p /data

ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["python", "-m", "metacrawl.web"]
