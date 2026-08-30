#!/usr/bin/env python3
"""
Phase 0 evidence: TLS fingerprint comparison against LinkedIn Voyager.

Same cookies, same headers, two HTTP clients:
  - requests  -> expect HTTP 999 (blocked on JA3 fingerprint)
  - curl_cffi -> expect HTTP 200 (Chrome TLS impersonation)

Usage:
    export LI_AT='...'
    export JSESSIONID='ajax:1234567890'
    python3 scripts/check_tls_fingerprint.py [public_id]

Writes evidence + raw profile JSON to docs/evidence/.
"""

import json
import os
import sys
import pathlib

EVIDENCE = pathlib.Path("docs/evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)

LI_AT = os.environ.get("LI_AT")
JSESSIONID = os.environ.get("JSESSIONID")

if not LI_AT or not JSESSIONID:
    sys.exit("Set LI_AT and JSESSIONID environment variables first.")

PUBLIC_ID = sys.argv[1] if len(sys.argv) > 1 else "williamhgates"
URL = f"https://www.linkedin.com/voyager/api/identity/profiles/{PUBLIC_ID}/profileView"

# csrf-token is JSESSIONID with surrounding quotes stripped.
CSRF = JSESSIONID.strip('"')

HEADERS = {
    "Host": "www.linkedin.com",                                    # mandatory outside a browser
    "csrf-token": CSRF,
    "x-restli-protocol-version": "2.0.0",
    "accept": "application/vnd.linkedin.normalized+json+2.1",      # flat included[] array
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


def record(label, status, body, note):
    path = EVIDENCE / f"tls-{label}.txt"
    path.write_text(
        f"client: {label}\n"
        f"url: {URL}\n"
        f"status: {status}\n"
        f"note: {note}\n"
        f"{'-' * 60}\n"
        f"{body[:2000]}\n"
    )
    print(f"  [{label}] HTTP {status} -> {path}")


def try_plain():
    print("\n[1/2] plain requests (expect 999)")
    try:
        import requests
        r = requests.get(URL, headers=HEADERS, cookies=COOKIES, timeout=30)
        record("plain-requests", r.status_code, r.text,
               "default JA3 fingerprint, no browser impersonation")
        return r.status_code
    except Exception as e:
        record("plain-requests", "ERROR", str(e), "request raised")
        return None


def try_impersonated():
    print("\n[2/2] curl_cffi chrome124 (expect 200)")
    try:
        from curl_cffi import requests as cffi
        r = cffi.get(URL, headers=HEADERS, cookies=COOKIES,
                     impersonate="chrome124", timeout=30)
        record("curl-cffi-chrome124", r.status_code, r.text,
               "Chrome TLS impersonation, identical cookies and headers")

        if r.status_code == 200:
            data = r.json()
            out = EVIDENCE / f"profileView-{PUBLIC_ID}.json"
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"\n  raw profile saved -> {out}")

            included = data.get("included", [])
            print(f"  included[] entities: {len(included)}")
            types = {}
            for item in included:
                t = item.get("$type", "unknown").split(".")[-1]
                types[t] = types.get(t, 0) + 1
            for t, n in sorted(types.items(), key=lambda x: -x[1])[:15]:
                print(f"    {n:>4}  {t}")
        return r.status_code
    except Exception as e:
        record("curl-cffi-chrome124", "ERROR", str(e), "request raised")
        return None


if __name__ == "__main__":
    print(f"target: {URL}")
    a = try_plain()
    b = try_impersonated()

    print("\n" + "=" * 60)
    print(f"plain requests : {a}")
    print(f"curl_cffi      : {b}")
    if a == 999 and b == 200:
        print("\nCONFIRMED: block is TLS-fingerprint based, not credential based.")
    elif b == 200:
        print(f"\ncurl_cffi works. Plain client returned {a} rather than 999 —")
        print("record the actual value; the finding still holds if they differ.")
    else:
        print("\nBoth failed. Check cookie freshness and the csrf-token quote stripping.")
    print("=" * 60)
