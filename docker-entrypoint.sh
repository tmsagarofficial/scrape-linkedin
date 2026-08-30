#!/bin/sh
# Prepare the cache directory before the application starts.
#
# The pre-seeded cache is scraped profile data, so it is never committed and
# never baked into an image layer. It is supplied at runtime through
# SEED_CACHE_FILE, pointing at a secret file the host mounts — which the
# application imports at startup when the cache is empty.
#
# This script only has to guarantee the cache directory exists and is writable;
# an unwritable CACHE_PATH is otherwise a confusing failure deep in a request.
set -e

CACHE="${CACHE_PATH:-/data/cache.db}"
mkdir -p "$(dirname "$CACHE")" 2>/dev/null || true

if [ ! -w "$(dirname "$CACHE")" ]; then
    echo "WARNING: $(dirname "$CACHE") is not writable; caching will fail" >&2
fi

exec "$@"
