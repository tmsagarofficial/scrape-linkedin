#!/usr/bin/env python3
"""Compare the available LinkedIn profile data sources head to head.

Three independent implementations of this same brief were found using
`GET /voyager/api/identity/dash/profiles`, an endpoint that appears in **none**
of the eight browser captures taken for this project. The flagship web client
never calls it, so traffic capture alone could not reveal it.

That raises two questions this script answers empirically rather than by
argument:

1. **Does `dash/profiles` still work?** If it does, it returns typed JSON —
   `{"month": 8, "year": 2022}` — where the SDUI path returns
   `"Aug 2022 - Jul 2025"` and must be parsed back with regex. Typed data is
   strictly better, and that would matter more than which approach is harder.

2. **Does it need TLS impersonation?** The other implementations use plain
   `httpx`/`undici` with no impersonation. The brief for this project states
   that a plain client is answered with HTTP 999 regardless of a valid cookie.
   Both cannot be true of the same endpoint, and the difference decides whether
   `curl_cffi` is load-bearing or ceremony.

Four variants are run against one target so the comparison is like-for-like:

    A  dash/profiles  + plain requests      what the other repos actually do
    B  dash/profiles  + curl_cffi chrome    does the endpoint work at all
    C  profileView    + curl_cffi chrome    the retired endpoint, as a control
    D  SDUI/RSC       + curl_cffi chrome    this project's path

Usage:
    export LI_AT='...' JSESSIONID='ajax:...'
    python3 scripts/compare_endpoints.py timcook
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.linkedin.client import LinkedInClient
from app.linkedin.rsc_parser import iter_text, parse_flight

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "docs" / "evidence" / "raw"
OUTPUT = REPO / "docs" / "evidence" / "endpoint-comparison.md"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


@dataclass
class Outcome:
    label: str
    endpoint: str
    client: str
    status: int | str = 0
    seconds: float = 0.0
    body_bytes: int = 0
    verdict: str = ""
    detail: str = ""
    typed_dates: bool | None = None
    entities: int = 0
    types: list[tuple[str, int]] = field(default_factory=list)


def voyager_headers(public_id: str, csrf: str) -> dict[str, str]:
    """The documented Voyager header set."""
    return {
        "Host": "www.linkedin.com",
        "csrf-token": csrf,
        "x-restli-protocol-version": "2.0.0",
        # Returns a flat included[] graph rather than deep nesting.
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "x-li-lang": "en_US",
        "x-li-track": json.dumps(
            {
                "clientVersion": "1.13.1665",
                "mpVersion": "1.13.1665",
                "osName": "web",
                "timezoneOffset": 5.5,
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
            },
            separators=(",", ":"),
        ),
        "user-agent": USER_AGENT,
        "referer": f"https://www.linkedin.com/in/{public_id}/",
    }


def classify(status: int, body: bytes) -> tuple[str, str]:
    """Describe the response without assuming it is what we hoped for."""
    head = body[:300].decode("utf-8", "ignore").lstrip()
    if status == 999:
        return "BLOCKED", "HTTP 999 — automation defence"
    if status == 410:
        return "GONE", "HTTP 410 — endpoint retired"
    if status in (401, 403):
        return "DENIED", f"HTTP {status} — session rejected for this resource"
    if status == 404:
        return "NOT FOUND", "HTTP 404"
    if status != 200:
        return "ERROR", f"HTTP {status}"
    if head.startswith(("0:", "1:", "2:")) or ":I[" in head[:80]:
        return "OK", "RSC flight stream"
    if head.startswith("{"):
        return "OK", "JSON"
    if head.lower().startswith(("<!doctype", "<html")):
        return "HTML", "server-rendered HTML, not data"
    return "UNKNOWN", head[:60]


def inspect_voyager(body: bytes, outcome: Outcome) -> None:
    """Look for the thing that actually matters: typed dates."""
    try:
        data = json.loads(body)
    except ValueError:
        return
    included = data.get("included") or []
    outcome.entities = len(included)
    outcome.types = Counter(
        item.get("$type", "?").rsplit(".", 1)[-1] for item in included
    ).most_common(6)

    # A typed date is a dict with numeric year — the whole advantage over SDUI.
    blob = json.dumps(data)
    outcome.typed_dates = '"year":' in blob or '"year": ' in blob


def run(public_id: str) -> list[Outcome]:
    csrf = settings.jsessionid.strip('"')
    cookies = {"li_at": settings.li_at, "JSESSIONID": settings.jsessionid}
    headers = voyager_headers(public_id, csrf)
    dash = (
        "https://www.linkedin.com/voyager/api/identity/dash/profiles"
        f"?q=memberIdentity&memberIdentity={public_id}"
    )
    legacy = (
        "https://www.linkedin.com/voyager/api/identity/profiles/"
        f"{public_id}/profileView"
    )
    results: list[Outcome] = []
    RAW.mkdir(parents=True, exist_ok=True)

    # -- A: dash/profiles with a plain client, as the other repos do ----------
    outcome = Outcome("A", "voyager identity/dash/profiles", "requests (no impersonation)")
    try:
        import requests as plain

        started = time.monotonic()
        response = plain.get(dash, headers=headers, cookies=cookies, timeout=30)
        outcome.seconds = time.monotonic() - started
        outcome.status = response.status_code
        outcome.body_bytes = len(response.content)
        outcome.verdict, outcome.detail = classify(response.status_code, response.content)
        if outcome.verdict == "OK":
            inspect_voyager(response.content, outcome)
            (RAW / "cmp_A_dash_plain.json").write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        outcome.status, outcome.verdict, outcome.detail = "ERR", "ERROR", str(exc)[:90]
    results.append(outcome)

    # -- B: dash/profiles with Chrome TLS impersonation -----------------------
    from curl_cffi import requests as cffi

    outcome = Outcome("B", "voyager identity/dash/profiles", "curl_cffi chrome142")
    try:
        started = time.monotonic()
        response = cffi.get(
            dash, headers=headers, cookies=cookies,
            impersonate=settings.impersonate, timeout=30,
        )
        outcome.seconds = time.monotonic() - started
        outcome.status = response.status_code
        outcome.body_bytes = len(response.content)
        outcome.verdict, outcome.detail = classify(response.status_code, response.content)
        if outcome.verdict == "OK":
            inspect_voyager(response.content, outcome)
            (RAW / "cmp_B_dash_cffi.json").write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        outcome.status, outcome.verdict, outcome.detail = "ERR", "ERROR", str(exc)[:90]
    results.append(outcome)

    # -- C: the retired endpoint, as a control --------------------------------
    outcome = Outcome("C", "voyager profileView (legacy)", "curl_cffi chrome142")
    try:
        started = time.monotonic()
        response = cffi.get(
            legacy, headers=headers, cookies=cookies,
            impersonate=settings.impersonate, timeout=30,
        )
        outcome.seconds = time.monotonic() - started
        outcome.status = response.status_code
        outcome.body_bytes = len(response.content)
        outcome.verdict, outcome.detail = classify(response.status_code, response.content)
    except Exception as exc:  # noqa: BLE001
        outcome.status, outcome.verdict, outcome.detail = "ERR", "ERROR", str(exc)[:90]
    results.append(outcome)

    # -- D: this project's SDUI/RSC path --------------------------------------
    outcome = Outcome("D", "SDUI screen (this project)", "curl_cffi chrome142")
    try:
        started = time.monotonic()
        shell, profile_id = LinkedInClient(settings).fetch_screen(public_id)
        outcome.seconds = time.monotonic() - started
        outcome.status = 200
        outcome.body_bytes = len(shell)
        outcome.verdict, outcome.detail = "OK", "RSC flight stream"
        nodes = list(iter_text(parse_flight(shell)))
        outcome.entities = len(nodes)
        # SDUI never carries typed dates; that is the known trade.
        outcome.typed_dates = False
        (RAW / "cmp_D_sdui_screen.bin").write_text(shell)
    except Exception as exc:  # noqa: BLE001
        outcome.status, outcome.verdict, outcome.detail = "ERR", "ERROR", str(exc)[:90]
    results.append(outcome)

    return results


def render(public_id: str, results: list[Outcome]) -> str:
    lines = [
        "# Endpoint comparison",
        "",
        f"Target: `{public_id}`. One request per variant, same session, "
        "same target, run back to back.",
        "",
        "| | Endpoint | Client | Status | Result | Bytes | Time |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| **{r.label}** | {r.endpoint} | {r.client} | `{r.status}` "
            f"| {r.verdict} — {r.detail} | {r.body_bytes:,} | {r.seconds:.1f}s |"
        )
    lines += ["", "## What each variant tests", ""]
    lines += [
        "* **A** is what the other implementations of this brief do: the Dash "
        "endpoint with an ordinary HTTP client and no TLS impersonation.",
        "* **B** isolates the endpoint from the client — same request, Chrome "
        "TLS fingerprint.",
        "* **C** is the control: the endpoint this project found retired.",
        "* **D** is this project's path.",
        "",
    ]

    a, b = results[0], results[1]
    lines += ["## Reading the result", ""]
    if a.verdict == "OK" and b.verdict == "OK":
        lines.append(
            "`dash/profiles` answers **with and without** TLS impersonation. "
            "`curl_cffi` is therefore not load-bearing for this endpoint, and "
            "the other implementations' simpler stacks are justified."
        )
    elif b.verdict == "OK" and a.verdict != "OK":
        lines.append(
            f"`dash/profiles` answers **only** with Chrome TLS impersonation "
            f"(plain client: {a.verdict}). This is direct evidence for the "
            "fingerprinting claim, and means the other implementations depend "
            "on behaviour that does not hold from every client or network."
        )
    elif a.verdict != "OK" and b.verdict != "OK":
        lines.append(
            "`dash/profiles` did **not** answer this session by either client. "
            "Endpoint availability varies by account, region and egress, so "
            "this is evidence about this session rather than proof the "
            "endpoint is gone."
        )

    typed = [r for r in results if r.typed_dates]
    if typed:
        lines += [
            "",
            "Where a Voyager variant succeeded it returned **typed** fields. "
            "That is a genuine advantage over the SDUI path, which serves "
            "pre-rendered display strings that must be recovered by regex — "
            "see `_meta.parse_confidence`.",
        ]
    for r in results:
        if r.types:
            lines += ["", f"Entity types in variant {r.label} "
                          f"({r.entities} entities):", ""]
            lines += [f"* `{name}` × {count}" for name, count in r.types]
    return "\n".join(lines) + "\n"


def main() -> int:
    public_id = sys.argv[1] if len(sys.argv) > 1 else "timcook"
    if not settings.has_session:
        sys.exit("Set LI_AT and JSESSIONID first. Nothing was sent.")

    print(f"target: {public_id}\nrequests: 4 (one per variant)\n")
    results = run(public_id)
    for r in results:
        typed = "" if r.typed_dates is None else (
            "  typed-dates" if r.typed_dates else "  display-strings"
        )
        print(
            f"  {r.label}  {r.client:<30} {str(r.status):<5} "
            f"{r.verdict:<10} {r.body_bytes:>8,}B {r.seconds:>5.1f}s{typed}"
        )
        if r.detail and r.verdict != "OK":
            print(f"       {r.detail}")

    OUTPUT.write_text(render(public_id, results))
    print(f"\nwrote {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
