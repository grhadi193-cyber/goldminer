# Mishkah worker — one container that runs the research worker AND serves the Mission Control panel.
#
# Why a Dockerfile instead of a buildpack: the image is the whole deployment story. It installs the
# Python worker, builds the static UI export (Next.js), and starts uvicorn — so a Render web service
# only needs environment variables, not a set of dashboard build steps that can drift.
#
#   docker build -t mishkah .
#   docker run -p 8787:8787 -e DATABASE_URL=... -e MISHKAH_MASTER_KEY=... mishkah
#
# Phase 1 is research + paper trading only: nothing in this image can place an order.

# ---------------------------------------------------------------- stage 1: build the panel UI
FROM node:20-bookworm-slim AS ui
WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
# The bundle is NOT baked in: the browser reads /api/bundle/* from the worker, which builds every
# member from the database. A build-time snapshot would go stale the moment the worker writes a row.
RUN npm run build

# ---------------------------------------------------------------- stage 2: the worker
FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY worker/pyproject.toml /app/worker/pyproject.toml
COPY worker/worker /app/worker/worker
RUN pip install --no-cache-dir -e /app/worker

COPY db/ /app/db/
COPY scripts/ /app/scripts/
COPY docs/ /app/docs/
COPY ui/package.json /app/ui/package.json
COPY ui/README.md /app/ui/README.md
COPY --from=ui /build/ui/out /app/ui/out

# Run as a non-root user; the only writable path the worker needs is /tmp (bundles are optional).
RUN useradd --create-home --uid 10001 mishkah \
 && chown -R mishkah:mishkah /app
USER mishkah

ENV WORKER_PORT=8787 \
    MISHKAH_BUNDLE_DIR=/tmp/mishkah-bundle

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WORKER_PORT}/healthz" || exit 1

# Migrations first (they are additive and idempotent), then the one long-running process.
CMD ["sh", "-lc", "python -m worker db up && python -m worker server run --host 0.0.0.0 --port ${WORKER_PORT}"]
