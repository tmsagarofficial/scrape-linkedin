#!/usr/bin/env python3
"""
Test whether the legacy Voyager profileView REST endpoint still works,
now that the LinkedIn web client itself has moved to SDUI/RSC calls.

Usage:
    export LI_AT='...'
    export JSESSIONID='ajax:<value from your own session>'   # from your HAR capture
    python3 scripts/check_profileview_410.py [public_id]
"""

import json
import os
import sys

LI_AT = os.environ.get("LI_AT")
JSESSIONID = os.environ.get("JSESSIONID")

if not LI_AT or not JSESSIONID:
    sys.exit("Set LI_AT and JSESSIONID environment variables first.")

PUBLIC_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "TEST_PUBLIC_ID", "williamhgates"
)
URL = f"https://www.linkedin.com/voyager/api/identity/profiles/{PUBLIC_ID}/profileView"

CSRF = JSESSIONID.strip('"')

HEADERS = {
    "Host": "www.linkedin.com",
    "csrf-token": CSRF,
    "x-restli-protocol-version": "2.0.0",
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "accept-language": "en-US,en;q=0.9",
    "x-li-lang": "en_US",
    "x-li-track": json.dumps({
        "clientVersion": "1.13.1665",
        "mpVersion": "1.13.1665",
        "osName": "web",
        "timezoneOffset": 5.5,
        "timezone": "Asia/Kolkata",
        "deviceFormFactor": "DESKTOP",
        "mpName": "voyager-web",
    }),
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "referer": f"https://www.linkedin.com/in/{PUBLIC_ID}/",
}

COOKIES = {"li_at": LI_AT, "JSESSIONID": JSESSIONID}

print(f"target: {URL}\n")

try:
    from curl_cffi import requests as cffi
except ImportError:
    sys.exit("curl_cffi not installed. Run: pip install curl_cffi --break-system-packages")

r = cffi.get(URL, headers=HEADERS, cookies=COOKIES, impersonate="chrome124", timeout=30)

print(f"STATUS: {r.status_code}\n")

if r.status_code == 200:
    data = r.json()
    out_path = "profileView_raw.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"saved raw response -> {out_path}")
    included = data.get("included", [])
    print(f"included[] entities: {len(included)}")
    types = {}
    for item in included:
        t = item.get("$type", "unknown").split(".")[-1]
        types[t] = types.get(t, 0) + 1
    for t, n in sorted(types.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:>4}  {t}")
    print("\nCONFIRMED: legacy profileView endpoint is still live.")
else:
    print("Body (first 1000 chars):")
    print(r.text[:1000])
    print("\nLegacy endpoint appears dead/blocked. This is also a real finding —")
    print("document it: browser no longer calls it, and direct access confirms")
    print("it's been retired/gated as part of the SDUI migration.")
