# Provider-neutral image. Runs anywhere that accepts a container and sets PORT:
# Fly.io, Render, Railway, Cloud Run, Koyeb, a plain VPS.
#
# Deliberately no provider-specific tooling, config or CLI baked in. What the
# host must supply is listed in DEPLOYMENT.md.

FROM python:3.12-slim AS base

# curl_cffi ships prebuilt wheels carrying its own patched libcurl, so no
# compiler toolchain is needed. ca-certificates is required for TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so application edits do not invalidate the layer.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app

# Optional: bake a pre-seeded cache into the image.
#
# Cloud Run's filesystem is ephemeral and there is no volume to mount, so a
# cache written at runtime dies with the instance. Baking the seed in means
# every instance starts able to answer for the seeded profiles even if the
# LinkedIn session has expired — which is the state a reviewer is most likely
# to arrive in, and the state that took down most comparable deployments.
#
# The file is gitignored, so this is a no-op unless it exists at build time.
# Build it first with: python3 scripts/seed_cache.py
COPY --chown=appuser:appuser seed-cache.db* /seed/

# The entrypoint restores a baked seed into CACHE_PATH on a cold start, so an
# ephemeral host still answers for the seeded profiles from the first request.
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run unprivileged. The cache directory must be writable by this user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default cache location. Point CACHE_PATH at a mounted volume to make the
# cache survive redeploys — without one it is rebuilt on every deploy, which
# costs LinkedIn requests and undermines the stale-if-error fallback.
ENV CACHE_PATH=/data/cache.db

# Hosts inject PORT; 8080 is the fallback for a plain `docker run`.
ENV PORT=8080
EXPOSE 8080

# /health is cheap by design: it reports session state from configuration and
# never calls LinkedIn, so a health probe cannot consume the rate limit.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/health\").read()"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
