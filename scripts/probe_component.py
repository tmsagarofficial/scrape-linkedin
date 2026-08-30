#!/usr/bin/env python3
"""Identify which profile section each SDUI component renders.

The profile screen shell enumerates its lazily-fetched components
(`profileCardsBelowActivityPart1` through `Part7`, plus several variants) but
does not say what each contains. This resolves that mapping empirically, one
component per request.

Every call is written to docs/evidence/request-log.jsonl before anything is
printed, so the audit trail is complete even if a run is interrupted.

Usage:
    export LI_AT='...' JSESSIONID='ajax:...'
    python3 scripts/probe_component.py jordan-rivera --parts 2
    python3 scripts/probe_component.py jordan-rivera --parts 2 3 4
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.linkedin import request_log
from app.linkedin.rsc_parser import iter_text, parse_flight

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "docs" / "evidence" / "raw"

BASE = "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
DSL = "com.linkedin.sdui.generated.profile.dsl.impl"
SCREEN_ID = "com.linkedin.sdui.flagshipnav.profile.Profile"
CLIENT_VERSION = "0.2.7003"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

#: The state keys observed in a real request body. Each is a BindingImpl whose
#: key is formulaic: ProfileComponentState<Name><vanity>ProfileComponentState.
_STATE_KEYS = [
    ("shouldRefreshScreenOnReappear", "ShouldRefreshScreen"),
    ("shouldFetchFromCache", "FetchFromCache"),
    ("shouldDisplayTabAnchors", "ShouldDisplayTabAnchors"),
    ("shouldReloadTopCardOnReappear", "ShouldReloadTopCardOnReappear"),
]


def binding(name: str, vanity: str) -> dict:
    return {
        "type": "com.linkedin.sdui.components.core.BindingImpl",
        "value": {
            "key": f"ProfileComponentState{name}{vanity}ProfileComponentState",
            "namespace": "MemoryNamespace",
        },
    }


def build_body(vanity: str, profile_id: str) -> str:
    """Reconstruct the component request body from the vanity name alone."""
    state = {"profileId": vanity}
    for field, name in _STATE_KEYS:
        state[field] = binding(name, vanity)
    return json.dumps(
        {
            "clientArguments": {
                "payload": {
                    "isSelfView": False,
                    "vanityName": vanity,
                    "replaceableSectionArgs": {
                        "vanityName": vanity,
                        "hideCardsForGoldenGate": False,
                        "shouldSetupReplaceableComponent": True,
                        "vieweeProfileId": profile_id,
                        "isSelfView": False,
                        "isSelfViewResolved": False,
                    },
                    "profileComponentState": state,
                },
                "states": [],
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
                "screenId": SCREEN_ID,
                "knownTemplateIds": [],
            }
        },
        separators=(",", ":"),
    )


def headers(vanity: str, csrf: str) -> dict[str, str]:
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    tracking = base64.b64encode(secrets.token_bytes(16)).decode()
    return {
        "Host": "www.linkedin.com",
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "csrf-token": csrf,
        "origin": "https://www.linkedin.com",
        "referer": f"https://www.linkedin.com/in/{vanity}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": UA,
        "x-li-application-version": CLIENT_VERSION,
        "x-li-pageforestid": trace,
        "x-li-traceparent": f"00-{trace}-{span}-00",
        "x-li-tracestate": f"LinkedIn={span}",
        "x-li-page-instance": f"urn:li:page:p_flagship3_profile_view_base;{tracking}",
        "x-li-page-instance-tracking-id": tracking,
        "x-li-rsc-stream": "true",
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
    }


def find_profile_id(vanity: str) -> str | None:
    """Recover the durable ACoAA... id from a previously saved screen response."""
    for path in sorted(RAW.glob("probe_*.bin")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"ACoAA[A-Za-z0-9_-]{20,}", text)
        if match:
            return match.group(0)[:39]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vanity")
    parser.add_argument("--parts", nargs="+", required=True,
                        help="part numbers, or full component names")
    parser.add_argument("--profile-id")
    parser.add_argument("--impersonate", default="chrome142")
    args = parser.parse_args()

    li_at, jsessionid = os.environ.get("LI_AT"), os.environ.get("JSESSIONID")
    if not li_at or not jsessionid:
        sys.exit("Set LI_AT and JSESSIONID first. Nothing was sent.")

    profile_id = args.profile_id or find_profile_id(args.vanity)
    if not profile_id:
        sys.exit("Could not determine the durable profile id; pass --profile-id.")

    from curl_cffi import requests as cffi

    csrf = jsessionid.strip('"')
    cookies = {"li_at": li_at, "JSESSIONID": jsessionid}
    body = build_body(args.vanity, profile_id)
    RAW.mkdir(parents=True, exist_ok=True)

    print(f"profile id : {profile_id[:12]}...")
    print(f"components : {len(args.parts)} request(s)\n")

    for part in args.parts:
        name = part if part.startswith("profileCards") else f"profileCardsBelowActivityPart{part}"
        component = f"{DSL}.{name}"
        url = f"{BASE}?componentId={component}&sduiid={component}"
        request_headers = headers(args.vanity, csrf)

        try:
            response = cffi.post(
                url, headers=request_headers, cookies=cookies, data=body,
                impersonate=args.impersonate, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - a probe reports, never crashes
            print(f"  {name:<42} ERROR {exc}")
            continue

        content = response.content or b""
        out = RAW / f"component_{name}.bin"
        if content:
            out.write_bytes(content)

        entry = request_log.record(
            method="POST", url=url, headers=request_headers, cookies=cookies,
            status=response.status_code,
            response_headers=dict(response.headers), body=content,
            request_body=body, impersonate=args.impersonate,
            note=f"component probe: {name}",
            saved_to=str(out.relative_to(REPO)) if content else None,
        )

        # The section identity is present even when a card renders nothing:
        # an empty section still declares its observabilityIdentifier and its
        # card componentKey. This is what makes mapping cheap.
        text = content.decode("utf-8", "replace")
        identifiers = sorted(set(re.findall(
            r"com\.linkedin\.sdui\.impl\.profile\.components\.([A-Za-z0-9_]+)", text)))
        cards = sorted(set(re.findall(r"profile\.card\.ref[A-Za-z0-9_-]{39}([A-Za-z]+)", text)))
        if identifiers or cards:
            print(f"       identity: {', '.join(identifiers) or '-'}"
                  f"  |  card: {', '.join(cards) or '-'}")
        if "$undefined" in text and not identifiers:
            print("       (no content)")

        sections = []
        if entry["response"]["classification"] == "RSC flight stream":
            flight = parse_flight(content.decode("utf-8", "replace"))
            nodes = list(iter_text(flight))
            seen = []
            for node in nodes:
                if node.section and node.section not in seen:
                    seen.append(node.section)
            sections = seen[:6]
            print(f"  {name:<42} {response.status_code} {len(content):>8}B  {len(nodes)} nodes")
            for section in sections:
                sample = next(n.text for n in nodes if n.section == section)
                print(f"       {section:<44} e.g. {sample[:44]}")
        else:
            print(f"  {name:<42} {response.status_code} {len(content):>8}B  "
                  f"{entry['response']['classification']}")

    print(f"\nlogged -> {request_log.LOG_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
