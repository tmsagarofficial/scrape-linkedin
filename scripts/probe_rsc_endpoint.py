#!/usr/bin/env python3
"""Probe the SDUI profile endpoint from a plain HTTP client (build step 4f).

Answers three questions the HAR alone cannot, in one run:

1. Does ``GET /flagship-web/in/{public_id}`` with ``x-li-rsc-stream: true``
   return an RSC stream to a non-browser client, or does it fall back to
   server-rendered HTML (or 999 / a login redirect)?
2. Is ``miniProfileUrn`` required, or will the vanity slug alone do? This
   matters because the durable ``ACoAA...`` id is not known up front for an
   arbitrary profile URL, so requiring it would force a bootstrap request.
3. Are ``x-li-pageforestid`` / ``x-li-page-instance`` / ``parentSpanId``
   validated server-side, or accepted whenever well-formed? The HAR shows
   pageforestid is byte-identical to the traceparent trace id, which suggests
   they are client-minted — this tests that directly by sending freshly
   generated values.

Nothing is sent until LI_AT and JSESSIONID are present in the environment.
Bodies are written to docs/evidence/raw/ (gitignored); only the summary table
is intended for METHODOLOGY.md.

Usage:
    export LI_AT='...'
    export JSESSIONID='ajax:...'
    python3 scripts/probe_rsc_endpoint.py [public_id] [--profile-id ACoAA...]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "docs" / "evidence" / "raw"

# Observed in the capture. Kept together so drift is obvious in one place.
CLIENT_VERSION = "0.2.7003"
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36"
)


def fresh_telemetry() -> dict[str, str]:
    """Generate plausible, well-formed telemetry ids.

    Shapes copied from the capture: pageforestid is 32 hex characters and is
    reused as the traceparent trace id; the page-instance tracking id is 16
    random bytes, base64-encoded.
    """
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    tracking_id = base64.b64encode(secrets.token_bytes(16)).decode()
    return {
        "x-li-pageforestid": trace_id,
        "x-li-traceparent": f"00-{trace_id}-{span_id}-00",
        "x-li-tracestate": f"LinkedIn={span_id}",
        "x-li-page-instance": f"urn:li:page:p_flagship3_profile_view_base;{tracking_id}",
        "x-li-page-instance-tracking-id": tracking_id,
    }


def build_headers(public_id: str, csrf: str, user_agent: str) -> dict[str, str]:
    """The header set for an RSC stream request."""
    headers = {
        # AGENTS.md §2.2 — mandatory outside a browser, else 400 invalid hostname.
        "Host": "www.linkedin.com",
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "csrf-token": csrf,
        "referer": f"https://www.linkedin.com/in/{public_id}/",
        # The header that selects RSC over server-rendered HTML.
        "x-li-rsc-stream": "true",
        # A fetch, not a document navigation — this pairing is what the browser
        # sends when it takes the RSC path.
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-li-application-version": CLIENT_VERSION,
        "x-li-track": json.dumps(
            {
                "clientVersion": CLIENT_VERSION,
                "mpVersion": CLIENT_VERSION,
                "osName": "web",
                "timezoneOffset": 5.5,
                "timezone": "Asia/Calcutta",
                "deviceFormFactor": "DESKTOP",
                "mpName": "flagship-web",
            },
            separators=(",", ":"),
        ),
        "user-agent": user_agent,
    }
    headers.update(fresh_telemetry())
    return headers


def classify(response) -> str:
    """Describe what came back, without assuming it is what we hoped for."""
    body = response.content or b""
    content_type = response.headers.get("content-type", "")
    head = body[:200].decode("utf-8", "ignore").lstrip()

    if response.status_code == 999:
        return "999 BLOCKED (TLS fingerprint / active defense — AGENTS.md §2.1)"
    if head.startswith(("0:", "1:")) or ":I[" in head[:80]:
        return "RSC flight stream"
    if head.lower().startswith(("<!doctype", "<html")):
        return "server-rendered HTML (fell back off the RSC path)"
    if "json" in content_type:
        return "JSON"
    return f"unrecognised ({content_type or 'no content-type'})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("public_id", nargs="?", default="williamhgates")
    parser.add_argument("--profile-id", help="durable ACoAA... id, if known")
    parser.add_argument("--impersonate", default="chrome142")
    parser.add_argument(
        "--all",
        action="store_true",
        help="run the full 4-request matrix; default is 1 request",
    )
    args = parser.parse_args()

    li_at = os.environ.get("LI_AT")
    jsessionid = os.environ.get("JSESSIONID")
    if not li_at or not jsessionid:
        sys.exit("Set LI_AT and JSESSIONID first. Nothing was sent.")

    try:
        from curl_cffi import requests as cffi
    except ImportError:
        sys.exit("curl_cffi not installed.")

    csrf = jsessionid.strip('"')
    cookies = {"li_at": li_at, "JSESSIONID": jsessionid}
    RAW.mkdir(parents=True, exist_ok=True)

    base = f"https://www.linkedin.com/flagship-web/in/{args.public_id}"
    urn = (
        f"urn:li:fs_miniProfile:{args.profile_id}" if args.profile_id else None
    )

    # Each case isolates one variable.
    cases = [
        ("slug only", base, UA_DESKTOP, args.impersonate),
        ("slug + skipRedirect", f"{base}?skipRedirect=true", UA_DESKTOP, args.impersonate),
    ]
    if urn:
        cases.append(
            (
                "slug + urn (full browser shape)",
                f"{base}?skipRedirect=true&miniProfileUrn={urn}",
                UA_DESKTOP,
                args.impersonate,
            )
        )
    cases.append(("android fingerprint", f"{base}?skipRedirect=true", UA_ANDROID, "chrome131_android"))

    # Default to the single most informative case. Escalating only on
    # failure keeps authenticated request volume to a minimum.
    if not args.all:
        cases = cases[:1]

    print(f"requests       : {len(cases)}")
    print(f"target profile : {args.public_id}")
    print(f"telemetry ids  : freshly generated (not replayed from the HAR)\n")

    results = []
    for name, url, user_agent, impersonate in cases:
        try:
            response = cffi.get(
                url,
                headers=build_headers(args.public_id, csrf, user_agent),
                cookies=cookies,
                impersonate=impersonate,
                timeout=30,
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001 - a probe must report, not crash
            print(f"  {name:<32} ERROR {exc}")
            results.append({"case": name, "error": str(exc)})
            continue

        kind = classify(response)
        size = len(response.content or b"")
        out = RAW / f"probe_{name.replace(' ', '_').replace('+', '')}.bin"
        if size:
            out.write_bytes(response.content)

        location = response.headers.get("location", "")
        print(f"  {name:<32} {response.status_code}  {size:>8}B  {kind}")
        if location:
            print(f"  {'':<32} -> redirect: {location[:90]}")

        results.append(
            {
                "case": name,
                "impersonate": impersonate,
                "status": response.status_code,
                "bytes": size,
                "kind": kind,
                "redirect": location,
            }
        )

    summary = REPO / "docs" / "evidence" / "rsc-endpoint-probe.json"
    summary.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {summary}")
    print(f"bodies -> {RAW}/ (gitignored)")

    if any(r.get("kind") == "RSC flight stream" for r in results):
        print("\nAt least one case returned RSC. Parse it with:")
        print("  python3 -c \"from app.linkedin.rsc_parser import *; ...\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
