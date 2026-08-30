#!/usr/bin/env python3
"""Export the cache as a seed file for hosts that build from git.

Cloud Run uploads from a local directory, so a seed can be baked into the
image. Render and similar build from the repository and cannot see local
files — and the seed must never be committed, because it is scraped profile
data. The gap is closed with a dashboard-managed secret file.

Two formats:

    --format json    readable, ~33 KB
    --format gz      gzip + base64, ~8 KB (default) — the difference between a
                     comfortable paste into a web form and an awkward one

Load it by pointing SEED_CACHE_FILE at the mounted path.

    python3 scripts/export_seed.py --out seed-cache.json.gz.b64
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cache import ProfileCache


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default="seed-cache.db")
    parser.add_argument("--out", default="seed-cache.json.gz.b64")
    parser.add_argument("--format", choices=("json", "gz"), default="gz")
    args = parser.parse_args()

    if not Path(args.cache).exists():
        sys.exit(f"no cache at {args.cache}; run scripts/seed_from_captures.py first")

    cache = ProfileCache(args.cache, ttl_seconds=2_592_000)
    seed = {}
    for entry in cache.entries():
        hit = cache.get(entry["public_id"], allow_stale=True)
        if hit:
            seed[entry["public_id"]] = hit[0]

    if not seed:
        sys.exit("cache is empty; nothing to export")

    raw = json.dumps(seed, ensure_ascii=False, separators=(",", ":"))
    payload = (
        raw if args.format == "json"
        else base64.b64encode(gzip.compress(raw.encode())).decode()
    )
    Path(args.out).write_text(payload)

    print(f"  {len(seed)} profiles -> {args.out} ({len(payload):,} bytes)")
    for public_id in seed:
        profile = seed[public_id]["profile"]
        print(f"    {public_id:<18} {profile['name']['full'][:22]:<24} "
              f"exp={len(profile['experience'])}")
    print("\nThis is scraped profile data. Paste it into a host's secret-file")
    print("store and point SEED_CACHE_FILE at it. Never commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
