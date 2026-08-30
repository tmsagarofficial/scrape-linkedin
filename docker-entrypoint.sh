#!/bin/sh
# Restore a pre-seeded cache before the app starts, if one is available.
#
# The seed is scraped profile data, so it must never live in the repository.
# That leaves two ways to get it to a host, and both are supported here:
#
#   1. Baked into the image  (/seed/seed-cache.db)
#      Works where the build uploads from a local directory — e.g.
#      `gcloud run deploy --source .` with a .gcloudignore that keeps it.
#
#   2. Base64 secret file    ($SEED_CACHE_B64_FILE)
#      Works where the build pulls from git and cannot see local files —
#      e.g. Render, which offers dashboard-managed Secret Files. SQLite is
#      binary, so it is carried base64-encoded.
#
# Either way an existing cache is never overwritten: on a host with a
# persistent disk this runs once, and on an ephemeral host it runs on every
# cold start, which is the point.
set -e

CACHE="${CACHE_PATH:-/data/cache.db}"

if [ ! -f "$CACHE" ]; then
    mkdir -p "$(dirname "$CACHE")"

    if [ -n "$SEED_CACHE_B64_FILE" ] && [ -f "$SEED_CACHE_B64_FILE" ]; then
        if base64 -d "$SEED_CACHE_B64_FILE" > "$CACHE" 2>/dev/null; then
            echo "seed cache restored from secret file -> $CACHE"
        else
            echo "WARNING: could not decode $SEED_CACHE_B64_FILE; starting empty" >&2
            rm -f "$CACHE"
        fi
    elif [ -f /seed/seed-cache.db ]; then
        cp /seed/seed-cache.db "$CACHE"
        echo "seed cache restored from image -> $CACHE"
    else
        echo "no seed cache available; starting with an empty cache"
    fi
fi

exec "$@"
