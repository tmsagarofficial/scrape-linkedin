#!/usr/bin/env python3
"""Measure field coverage across a sample of profiles and emit COVERAGE.md.

AGENTS.md §10 asks for per-field fill rate, latency percentiles and failure
counts by error class, measured against deliberately awkward profiles rather
than convenient ones.

Two things this deliberately does *not* do:

* **It does not invent a denominator.** A field can be absent because LinkedIn
  did not return it or because the member does not have it, and those are
  different facts. Fill rate is reported as "of profiles fetched", and sections
  LinkedIn reported as empty are counted separately from sections that failed.
* **It does not hide the sample size.** A fill rate over six profiles is a very
  different claim from one over fifty, and the report says which it is.

Every profile costs several upstream requests, so the sample is explicit and
capped rather than open-ended.

Usage:
    python3 scripts/coverage_report.py --limit 6
    python3 scripts/coverage_report.py --limit 6 --complete
    python3 scripts/coverage_report.py --dry-run     # show the plan, send nothing
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
OUTPUT = REPO / "COVERAGE.md"

#: Scalar fields, and how to decide whether one was populated.
SCALAR_FIELDS = ("name", "headline", "location", "about", "follower_count")

#: List fields, reported both as "any entries" and as a mean count.
LIST_FIELDS = (
    "experience", "education", "skills", "certifications",
    "languages", "courses", "honors", "volunteer_causes",
)


@dataclass
class Result:
    public_id: str
    ok: bool = False
    error: str | None = None
    detail: str | None = None
    seconds: float = 0.0
    scalars: dict[str, bool] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)
    warnings: int = 0


def read_seeds(limit: int | None) -> list[str]:
    if not SEEDS.exists():
        sys.exit(f"missing {SEEDS}; add one profile URL or slug per line")
    ids = []
    for line in SEEDS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        try:
            ids.append(public_id_from_url(line))
        except InvalidProfileURL as exc:
            print(f"  skipping {line!r}: {exc}")
    return ids[:limit] if limit else ids


def measure(client: LinkedInClient, public_id: str, complete: bool) -> Result:
    result = Result(public_id=public_id)
    started = time.monotonic()
    try:
        fetched = client.fetch_profile(
            public_id, DEFAULT_SECTIONS, complete=complete
        )
    except LinkedInError as exc:
        result.seconds = time.monotonic() - started
        result.error = type(exc).__name__
        result.detail = str(exc)[:120]
        return result
    except Exception as exc:  # noqa: BLE001 - a survey must not abort midway
        result.seconds = time.monotonic() - started
        result.error = f"unexpected:{type(exc).__name__}"
        result.detail = str(exc)[:120]
        return result

    result.seconds = time.monotonic() - started
    response = build_profile(
        public_id, fetched.nodes, endpoint_used="coverage",
        components_used=fetched.components_used, profile_id=fetched.profile_id,
    )
    profile = response.profile
    result.ok = True
    result.warnings = len(response.meta.warnings) + len(fetched.warnings)

    result.scalars = {
        "name": bool(profile.name.full and profile.name.full != public_id),
        "headline": bool(profile.headline),
        "location": profile.location is not None,
        "about": bool(profile.about),
        "follower_count": profile.follower_count is not None,
    }
    result.counts = {name: len(getattr(profile, name)) for name in LIST_FIELDS}
    result.truncated = [
        key for key, cov in response.meta.coverage.items() if cov.truncated
    ]
    return result


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def render(results: list[Result], complete: bool) -> str:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    total = len(results)
    latencies = [r.seconds for r in ok]

    lines = [
        "# Coverage report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"by `scripts/coverage_report.py`.",
        "",
        f"**Sample: {total} profiles.** Mode: "
        f"{'`?complete=true` (paged full lists)' if complete else 'default (cards)'}.",
        "",
    ]

    if total < 20:
        lines += [
            "> This is a small sample, and the numbers below should be read as "
            "indicative rather than as rates. Each profile costs several "
            "authenticated upstream requests, so the survey is deliberately "
            "capped; AGENTS.md §10 asks for ~50, which is a request budget "
            "decision rather than a technical limit.",
            "",
        ]

    lines += [
        "## Outcome",
        "",
        "| | Count |",
        "|---|---|",
        f"| Fetched successfully | {len(ok)} |",
        f"| Failed | {len(failed)} |",
        "",
    ]

    if failed:
        lines += ["### Failures by error class", "", "| Error | Count |", "|---|---|"]
        for name, count in Counter(r.error for r in failed).most_common():
            lines.append(f"| `{name}` | {count} |")
        lines.append("")
        lines += ["Detail:", ""]
        for r in failed:
            lines.append(f"* `{r.public_id}` — {r.error}: {r.detail or 'no detail'}")
        lines.append("")

    if latencies:
        lines += [
            "## Latency",
            "",
            "End-to-end fetch, all sections, excluding mapping.",
            "",
            "| Percentile | Seconds |",
            "|---|---|",
            f"| p50 | {percentile(latencies, 50):.2f} |",
            f"| p95 | {percentile(latencies, 95):.2f} |",
            f"| max | {max(latencies):.2f} |",
            f"| mean | {statistics.fmean(latencies):.2f} |",
            "",
        ]

    if ok:
        lines += [
            "## Field fill rate",
            "",
            "Share of **successfully fetched** profiles where the field was "
            "populated. A low rate is not necessarily a defect — many members "
            "genuinely have no certifications — so the mean count is shown "
            "alongside it.",
            "",
            "| Field | Filled | Rate | Mean count |",
            "|---|---|---|---|",
        ]
        for name in SCALAR_FIELDS:
            filled = sum(1 for r in ok if r.scalars.get(name))
            lines.append(
                f"| `{name}` | {filled}/{len(ok)} | {filled / len(ok):.0%} | — |"
            )
        for name in LIST_FIELDS:
            filled = sum(1 for r in ok if r.counts.get(name))
            mean = statistics.fmean(r.counts.get(name, 0) for r in ok)
            lines.append(
                f"| `{name}` | {filled}/{len(ok)} | {filled / len(ok):.0%} "
                f"| {mean:.1f} |"
            )
        lines.append("")

        truncated = Counter(k for r in ok for k in r.truncated)
        lines += [
            "## Truncation",
            "",
            (
                "Sections LinkedIn returned only partially. With "
                "`--complete` these are paged out; the residue is sections "
                "whose true total LinkedIn never states."
                if complete
                else "Sections LinkedIn capped. Re-run with `--complete` to page "
                "the full lists."
            ),
            "",
            "| Section | Profiles truncated |",
            "|---|---|",
        ]
        for name, count in truncated.most_common():
            lines.append(f"| `{name}` | {count}/{len(ok)} |")
        if not truncated:
            lines.append("| _none_ | 0 |")
        lines.append("")

        lines += [
            "## Per profile",
            "",
            "A blank scalar cell means the field was absent for that profile. "
            "That is shown per profile rather than only as a rate, because an "
            "aggregate cannot distinguish a parser gap from a member who "
            "genuinely has no location set.",
            "",
            "| Profile | Time | Loc | Foll | About | Exp | Edu | Skills | Certs "
            "| Langs | Warn |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in ok:
            c, sc = r.counts, r.scalars
            tick = lambda name: "yes" if sc.get(name) else "—"  # noqa: E731
            lines.append(
                f"| `{r.public_id}` | {r.seconds:.1f}s "
                f"| {tick('location')} | {tick('follower_count')} "
                f"| {tick('about')} "
                f"| {c.get('experience', 0)} | {c.get('education', 0)} "
                f"| {c.get('skills', 0)} | {c.get('certifications', 0)} "
                f"| {c.get('languages', 0)} | {r.warnings} |"
            )
        lines.append("")

    lines += [
        "## Method",
        "",
        "Each profile is fetched through the same client the API uses, then "
        "mapped through the same mapper, so these numbers reflect what a caller "
        "would actually receive. Every request is recorded in "
        "`docs/evidence/request-log.jsonl`.",
        "",
        "A field counts as filled only if it is non-empty after mapping. `name` "
        "additionally does not count when it fell back to the URL slug, which "
        "is what happens when LinkedIn omits the top card.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--pace", type=float, default=3.0,
        help="seconds to wait between profiles; keeps the burst well under the "
             "outbound rate limit the API itself enforces",
    )
    args = parser.parse_args()

    ids = read_seeds(args.limit)
    if not ids:
        sys.exit("no usable profiles in seeds.txt")

    # Sections share components, so the cost is the number of *distinct*
    # components plus the screen shell — not the number of sections.
    per_profile = len(
        {SECTION_COMPONENTS[s] for s in DEFAULT_SECTIONS if s in SECTION_COMPONENTS}
    ) + 1
    print(f"profiles     : {len(ids)}")
    print(f"mode         : {'complete (paged)' if args.complete else 'cards'}")
    print(f"est. requests: ~{len(ids) * per_profile}"
          f"{' plus paging' if args.complete else ''}")
    print()

    if args.dry_run:
        for public_id in ids:
            print(f"  would fetch {public_id}")
        print("\nDry run: nothing was sent.")
        return 0

    if not settings.has_session:
        sys.exit("Set LI_AT and JSESSIONID first. Nothing was sent.")

    client = LinkedInClient(settings)
    results = []
    for index, public_id in enumerate(ids, 1):
        if index > 1 and args.pace:
            time.sleep(args.pace)
        result = measure(client, public_id, args.complete)
        results.append(result)
        state = "ok" if result.ok else f"FAILED {result.error}"
        print(f"  [{index}/{len(ids)}] {public_id:<28} {result.seconds:>5.1f}s  {state}")

    OUTPUT.write_text(render(results, args.complete))
    print(f"\nwrote {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
