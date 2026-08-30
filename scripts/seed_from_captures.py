#!/usr/bin/env python3
"""Rebuild the demo cache from saved captures, with no network access.

The live seeding path needs a working LinkedIn session. When the session is
blocked or expired that path is unavailable — which is precisely when a seeded
cache matters most. This rebuilds it offline from responses already captured.

Two reasons to prefer this over an older `cache.db`:

* Captures are re-mapped through the **current** parser and mapper, so entries
  pick up every fix made since they were first seeded — the Voyager identity
  merge, the multi-root walk, the location anchor, the About paragraph join.
* It costs nothing and cannot fail because of a blocked session.

Only profiles the operator is willing to serve publicly are included. Two
profiles present in the captures are deliberately excluded: they belong to
students rather than public figures, and pre-loading a stranger's data into a
public demo is a different act from fetching it when someone asks.

    python3 scripts/seed_from_captures.py            # writes cache.db
    python3 scripts/seed_from_captures.py --out seed-cache.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cache import ProfileCache
from app.linkedin.rsc_parser import iter_text, parse_flight
from app.linkedin.voyager import parse_identity
from app.normalize.mapper import build_profile

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "docs" / "evidence" / "raw"

#: public_id -> (RSC capture files, optional Voyager identity JSON)
#:
#: Restricted to public figures and the repository owner's own profile. The
#: captures also contain two private individuals; they are not listed, because
#: seeding them would publish their data rather than merely serve it on request.
PROFILES: dict[str, tuple[list[str], str | None]] = {
    "williamhgates": (["wg_screen.bin"], None),
    "reidhoffman": (["rh_screen.bin"], None),
    "rajshamani": (["raj_screen.bin"], None),
    "cooktim": (["cmp_D_sdui_screen.bin"], "cmp_B_dash_cffi.json"),
    "tmsagarofficial": (
        [
            "self_screen.bin",
            "har3_self_profileCardsAboveActivity.bin",
            "self_part1.bin",
            "self_part7.bin",
            "har3_self_profileCardsBelowActivityPart4.bin",
        ],
        None,
    ),
}


def load(public_id: str, files: list[str], identity_file: str | None):
    nodes, used = [], []
    for name in files:
        path = RAW / name
        if not path.exists():
            continue
        nodes.extend(
            iter_text(parse_flight(path.read_text(encoding="utf-8", errors="replace")))
        )
        used.append(name.replace(".bin", "").replace(".txt", ""))

    identity = {}
    if identity_file and (RAW / identity_file).exists():
        identity = parse_identity(json.loads((RAW / identity_file).read_text()))

    if not nodes and not identity:
        return None

    response = build_profile(
        public_id,
        nodes,
        endpoint_used="rebuilt offline from saved captures",
        components_used=used,
        identity=identity,
    )
    payload = response.model_dump(by_alias=True)
    meta = payload["_meta"]
    meta["seeded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["source"] = "cache"
    meta["warnings"].append(
        "seeded offline from a saved capture; fetch with ?refresh=true for live data"
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="cache.db")
    args = parser.parse_args()

    cache = ProfileCache(args.out, ttl_seconds=86_400)
    written = 0

    for public_id, (files, identity_file) in PROFILES.items():
        payload = load(public_id, files, identity_file)
        if payload is None:
            print(f"  {public_id:<18} skipped — no capture on disk")
            continue
        cache.put(public_id, payload)
        written += 1
        profile = payload["profile"]
        print(
            f"  {public_id:<18} {profile['name']['full'][:24]:<26} "
            f"exp={len(profile['experience']):<2} edu={len(profile['education']):<2} "
            f"skills={len(profile['skills']):<2} "
            f"about={'y' if profile.get('about') else '-'} "
            f"img={len(profile.get('images') or {})}"
        )

    print(f"\nwrote {written} profiles -> {args.out}")
    print("Verify with: python3 scripts/seed_cache.py --verify")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
