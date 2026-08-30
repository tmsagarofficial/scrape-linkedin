#!/usr/bin/env python3
"""Extract SDUI/RSC evidence from a browser HAR capture.

Enumerates every ``rsc-action`` call in a HAR, decodes the response bodies, and
writes the artifacts referenced by METHODOLOGY.md. Reproducible so that a
reviewer can regenerate the evidence from their own capture.

Raw bodies contain a real person's profile data. They are written to
``docs/evidence/raw/``, which is gitignored; only the component catalogue and a
scrubbed fixture are intended for commit.

Usage:
    python3 scripts/extract_rsc_from_har.py www.linkedin.com.har
"""

from __future__ import annotations

import base64
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "docs" / "evidence"
RAW = EVIDENCE / "raw"


def decode_body(entry: dict) -> str | None:
    """Return an entry's response body as text, if it has one."""
    content = entry["response"].get("content", {})
    text = content.get("text")
    if not text:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return text


def main(har_path: str) -> int:
    har = json.loads(Path(har_path).read_text())
    entries = har["log"]["entries"]
    RAW.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for index, entry in enumerate(entries):
        url = entry["request"]["url"]
        if "rsc-action" not in url:
            continue

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        component = (params.get("componentId") or params.get("sduiid") or [""])[0]

        body = decode_body(entry)
        out = RAW / f"entry{index}_{component.rsplit('.', 1)[-1] or 'unknown'}.txt"
        if body:
            out.write_text(body)

        rows.append(
            {
                "entry": index,
                "kind": parsed.path.rsplit("/", 1)[-1],
                "component": component,
                "status": entry["response"]["status"],
                "bytes": len(body) if body else 0,
                "file": out.name if body else "",
            }
        )

    # A quick census of what else the page talked to, to support the claim that
    # the legacy profile endpoint is never called.
    hosts = Counter()
    profileview = 0
    for entry in entries:
        url = entry["request"]["url"]
        hosts[urlparse(url).path.split("/")[1] if "/" in url[8:] else "-"] += 1
        if "profileView" in url:
            profileview += 1

    print(f"total HAR entries      : {len(entries)}")
    print(f"rsc-action calls       : {len(rows)}")
    print(f"legacy profileView hits: {profileview}")
    print()
    for row in rows:
        print(
            f"  [{row['entry']:>3}] {row['status']} {row['kind']:<15} "
            f"{row['component'].rsplit('.', 1)[-1]:<40} {row['bytes']:>7}B"
        )

    (EVIDENCE / "rsc-calls.json").write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {EVIDENCE / 'rsc-calls.json'}")
    print(f"wrote raw bodies -> {RAW}/ (gitignored)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1]))
