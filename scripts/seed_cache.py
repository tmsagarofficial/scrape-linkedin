#!/usr/bin/env python3
"""Populate the cache from seeds.txt so the demo survives session expiry.

AGENTS.md §7 states the design assumption plainly: **the session will be dead
when a reviewer opens this.** `LI_AT` lasts weeks, submissions are read later
than that, and a live fetch is the one part of this system that cannot be made
reliable. A pre-seeded cache is what keeps the API answering with real data once
the session stops working.

§9 governs what may be committed: the URLs in `seeds.txt` are fine, the scraped
bodies are not. The database this writes is gitignored and must be shipped to
the host out of band or regenerated on deploy.

    python3 scripts/seed_cache.py --dry-run     # show the plan, send nothing
    python3 scripts/seed_cache.py               # seed everything in seeds.txt
    python3 scripts/seed_cache.py --complete    # full lists, more requests
    python3 scripts/seed_cache.py --verify      # prove it works with no session

Leave at least one well-known profile *out* of seeds.txt, so `?refresh=true`
against it still demonstrates live fetching rather than replaying a seed.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cache import ProfileCache
from app.config import settings
from app.linkedin.client import (
    DEFAULT_SECTIONS,
    SECTION_COMPONENTS,
    LinkedInClient,
    LinkedInError,
)
from app.linkedin.urls import InvalidProfileURL, public_id_from_url
from app.normalize.mapper import build_profile

REPO = Path(__file__).resolve().parent.parent
SEEDS = REPO / "seeds.txt"


def read_seeds() -> list[str]:
    if not SEEDS.exists():
        sys.exit(f"missing {SEEDS}")
    ids: list[str] = []
    for line in SEEDS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        try:
            ids.append(public_id_from_url(line))
        except InvalidProfileURL as exc:
            print(f"  skipping {line!r}: {exc}")
    return ids


def verify(cache: ProfileCache) -> int:
    """Read every cached entry back with the session disabled.

    This is the gate that matters. Seeding that has not been proven to work
    without credentials is an untested fallback, which is the same as no
    fallback — and it is precisely the state the reviewer will arrive in.
    """
    stats = cache.stats()
    print(f"cached entries: {stats['entries']}")
    if not stats["entries"]:
        print("nothing cached; run without --verify first")
        return 1

    import app.main as main

    main.cache = cache
    main.settings.li_at = ""
    main.settings.jsessionid = ""

    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    headers = {"X-API-Key": main.settings.api_key}

    print("\nsession disabled; serving from cache only:\n")
    ok = True
    for public_id in read_seeds():
        response = client.get(f"/v1/profile/{public_id}", headers=headers)
        body = response.json()
        if response.status_code == 200 and body.get("profile"):
            meta = body["_meta"]
            name = body["profile"]["name"]["full"]
            print(
                f"  {public_id:<24} {response.status_code}  "
                f"source={meta['source']:<6} age={meta['cache_age_seconds']}s  {name}"
            )
        else:
            ok = False
            print(f"  {public_id:<24} {response.status_code}  NOT SERVED")

    print("\n" + ("Verified: the API answers with no session." if ok
                  else "FAILED: some profiles were not served from cache."))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--pace", type=float, default=3.0)
    args = parser.parse_args()

    cache = ProfileCache(settings.cache_path, settings.cache_ttl_seconds)

    if args.verify:
        return verify(cache)

    ids = read_seeds()
    per_profile = len(
        {SECTION_COMPONENTS[s] for s in DEFAULT_SECTIONS if s in SECTION_COMPONENTS}
    ) + 1
    print(f"cache        : {settings.cache_path}")
    print(f"profiles     : {len(ids)}")
    print(f"est. requests: ~{len(ids) * per_profile}"
          f"{' plus paging' if args.complete else ''}\n")

    if args.dry_run:
        for public_id in ids:
            print(f"  would seed {public_id}")
        print("\nDry run: nothing was sent.")
        return 0

    if not settings.has_session:
        sys.exit("Set LI_AT and JSESSIONID first. Nothing was sent.")

    client = LinkedInClient(settings)
    seeded = failed = 0

    for index, public_id in enumerate(ids, 1):
        if index > 1 and args.pace:
            time.sleep(args.pace)
        try:
            result = client.fetch_profile(
                public_id, DEFAULT_SECTIONS, complete=args.complete
            )
        except LinkedInError as exc:
            failed += 1
            print(f"  [{index}/{len(ids)}] {public_id:<24} FAILED {exc}")
            continue

        response = build_profile(
            public_id, result.nodes, endpoint_used="seed",
            components_used=result.components_used, profile_id=result.profile_id,
        )
        payload = response.model_dump(by_alias=True)
        payload["_meta"]["warnings"].extend(result.warnings)
        payload["_meta"]["complete"] = args.complete
        payload["_meta"]["seeded_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        cache.put(public_id, payload)
        seeded += 1
        print(
            f"  [{index}/{len(ids)}] {public_id:<24} ok  "
            f"{len(response.profile.experience)} exp, "
            f"{len(response.profile.skills)} skills"
        )

    print(f"\nseeded {seeded}, failed {failed} -> {settings.cache_path}")
    print("Verify with: python3 scripts/seed_cache.py --verify")
    print(
        "\nThe cache file is gitignored (§9). Ship it to the host out of band, "
        "or run this on deploy."
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
