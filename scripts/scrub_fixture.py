#!/usr/bin/env python3
"""Build a committable test fixture from a captured RSC response.

AGENTS.md §8 requires fixtures to be scrubbed before commit, and §9 forbids
committing harvested profile bodies to a public repo. The capture we developed
against is a real third party's profile, so it cannot ship as-is.

This applies a deterministic substitution map to produce a fixture that is
structurally identical to the real response — same record graph, same reference
forms, same separator characters, same section metadata — but describes a person
who does not exist. The parser exercises exactly the same code paths.

Usage:
    python3 scripts/scrub_fixture.py \
        docs/evidence/raw/entry115_profileCardsBelowActivityPart1.txt \
        tests/fixtures/rsc_below_activity_part1.txt

The substitution map is deliberately **not** in this file. A map of
``real value -> fake value`` is itself a disclosure of the real values, so
committing it would leak precisely what the scrubbing exists to remove. It lives
in ``scripts/scrub_map.local.json`` (gitignored); see
``scripts/scrub_map.example.json`` for the shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Real value -> synthetic replacement, kept out of version control.
MAP_PATH = Path(__file__).parent / "scrub_map.local.json"


def load_substitutions() -> dict[str, str]:
    """Load the scrub map, failing loudly if it is absent."""
    if not MAP_PATH.exists():
        raise SystemExit(
            f"missing {MAP_PATH}\n"
            f"Copy {MAP_PATH.with_name('scrub_map.example.json')} and fill in the\n"
            "real values from your own capture. It is gitignored by design."
        )
    return json.loads(MAP_PATH.read_text())


def scrub(text: str, substitutions: dict[str, str]) -> str:
    """Apply every substitution, longest key first.

    Longest-first ordering stops a short key from clobbering a longer one that
    contains it (e.g. a surname inside a full name).
    """
    for real in sorted(substitutions, key=len, reverse=True):
        text = text.replace(real, substitutions[real])
    return text


def main(src: str, dst: str) -> int:
    substitutions = load_substitutions()
    text = Path(src).read_text()
    scrubbed = scrub(text, substitutions)

    leaked = [real for real in substitutions if real in scrubbed]
    if leaked:
        print(f"REFUSING TO WRITE: {len(leaked)} value(s) survived scrubbing")
        return 1

    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(scrubbed)
    print(f"wrote {out} ({len(scrubbed)} chars, {len(substitutions)} substitutions)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
