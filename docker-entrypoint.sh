#!/bin/sh
# Seed the cache from the image, if one was baked in and none exists yet.
#
# On a host with a persistent volume this runs once and then never again. On an
# ephemeral host such as Cloud Run it runs on every cold start, which is the
# point: the demo answers for the seeded profiles from the first request,
# whether or not the LinkedIn session is still alive.
set -e

if [ -f /seed/seed-cache.db ] && [ ! -f "${CACHE_PATH:-/data/cache.db}" ]; then
    mkdir -p "$(dirname "${CACHE_PATH:-/data/cache.db}")"
    cp /seed/seed-cache.db "${CACHE_PATH:-/data/cache.db}"
    echo "seeded cache restored from image -> ${CACHE_PATH:-/data/cache.db}"
fi

exec "$@"
