#!/usr/bin/env python3
"""Assemble a full profile from saved RSC captures, with no network access.

Reads every component body already saved under docs/evidence/raw/, parses them
in card order, and maps the result into the public schema. Used to exercise the
whole pipeline offline.

Usage:
    python3 scripts/build_profile_from_captures.py jordan-rivera
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

from app.linkedin.rsc_parser import iter_text, parse_flight
from app.normalize.mapper import build_profile

_PROFILE_ID_RE = re.compile(r"ACoAA[A-Za-z0-9_-]{20,}")

RAW = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "raw"

# Screen shell first (top card), then the cards in the order they render.
ORDER = [
    "probe_slug_only.bin",
    # Part1 came from the HAR export rather than a live probe, so it carries the
    # extractor's naming.
    "entry115_profileCardsBelowActivityPart1.txt",
] + [f"component_profileCardsBelowActivityPart{n}.bin" for n in range(2, 8)]


def main(public_id: str) -> int:
    nodes, used, profile_id = [], [], None
    for name in ORDER:
        path = RAW / name
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        if profile_id is None:
            match = _PROFILE_ID_RE.search(body)
            if match:
                profile_id = match.group(0)[:39]
        flight = parse_flight(body)
        found = list(iter_text(flight))
        nodes.extend(found)
        used.append(name.replace(".bin", ""))
        print(f"  {name:<52} {len(flight.data):>4} records  {len(found):>4} nodes")

    response = build_profile(
        public_id, nodes,
        endpoint_used="flagship-web/in/{public_id} + rsc-action components",
        components_used=used,
        profile_id=profile_id,
    )
    out = Path("profile_output.json")
    out.write_text(json.dumps(response.model_dump(by_alias=True), indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "jordan-rivera"))
