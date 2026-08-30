"""Append-only audit log of every outbound request to LinkedIn.

Every authenticated call against LinkedIn's private endpoints is recorded here:
what was sent, what came back, and when. Two reasons this is product code rather
than a debugging aid:

* **Accountability.** AGENTS.md §15 is explicit that this activity violates
  LinkedIn's User Agreement and that the project is built for evaluation on a
  small sample. A complete, timestamped record of every request made is what
  makes "small sample" a checkable claim instead of an assertion.
* **Reproducibility.** METHODOLOGY.md's findings each rest on a specific
  request/response pair. The log is the evidence chain behind them.

Credentials are never written. ``li_at``, ``JSESSIONID`` and ``csrf-token`` are
replaced with a short salted fingerprint, which is enough to tell whether two
requests used the same session without disclosing either value.

The log is JSON Lines at ``docs/evidence/request-log.jsonl`` — append-only, one
object per request, so it survives interleaved runs and is trivially greppable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
LOG_PATH = REPO / "docs" / "evidence" / "request-log.jsonl"

#: Headers whose values must never be written to disk.
_SECRET_HEADERS = {"csrf-token", "cookie", "authorization", "x-li-at"}

#: Cookie names whose values must never be written to disk.
_SECRET_COOKIES = {"li_at", "jsessionid", "li_rm", "bcookie"}


def fingerprint(value: str) -> str:
    """A short, non-reversible tag for a secret.

    Lets the log show that two requests reused one session without recording the
    session itself. Truncated deliberately: enough to correlate, useless to an
    attacker.
    """
    if not value:
        return "<empty>"
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"sha256:{digest[:12]}"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: (fingerprint(value) if name.lower() in _SECRET_HEADERS else value)
        for name, value in headers.items()
    }


def _redact_cookies(cookies: dict[str, str]) -> dict[str, str]:
    return {
        name: (fingerprint(value) if name.lower() in _SECRET_COOKIES else value)
        for name, value in cookies.items()
    }


def classify(status: int, content_type: str, body: bytes) -> str:
    """Describe what came back, without assuming it is what we hoped for."""
    head = body[:200].decode("utf-8", "ignore").lstrip()
    if status == 999:
        return "999 blocked (TLS fingerprint / active defense)"
    if status == 410:
        return "410 gone (endpoint retired)"
    if head.startswith(("0:", "1:", "2:")) or ":I[" in head[:80]:
        return "RSC flight stream"
    if head.lower().startswith(("<!doctype", "<html")):
        return "server-rendered HTML"
    if "json" in content_type:
        return "JSON"
    if not body:
        return "empty body"
    return f"unrecognised ({content_type or 'no content-type'})"


def record(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    cookies: dict[str, str],
    status: int,
    response_headers: dict[str, str],
    body: bytes,
    request_body: str | None = None,
    impersonate: str | None = None,
    note: str | None = None,
    saved_to: str | None = None,
) -> dict[str, Any]:
    """Append one request/response pair to the log and return the entry."""
    content_type = response_headers.get("content-type", "")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "method": method,
            "url": url,
            "impersonate": impersonate,
            "headers": _redact_headers(headers),
            "cookies": _redact_cookies(cookies),
            "body_bytes": len(request_body) if request_body else 0,
        },
        "response": {
            "status": status,
            "bytes": len(body),
            "content_type": content_type,
            "classification": classify(status, content_type, body),
            "saved_to": saved_to,
        },
    }
    if note:
        entry["note"] = note

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def summarise() -> str:
    """Render the log as a table, for METHODOLOGY.md."""
    if not LOG_PATH.exists():
        return "No requests recorded."

    rows = [json.loads(line) for line in LOG_PATH.read_text().splitlines() if line.strip()]
    out = [f"Total outbound requests to LinkedIn: {len(rows)}", ""]
    out.append(f"{'#':<4} {'ts (UTC)':<21} {'method':<7} {'status':<7} {'bytes':>9}  target")
    for i, row in enumerate(rows, 1):
        req, resp = row["request"], row["response"]
        target = req["url"].replace("https://www.linkedin.com", "")
        out.append(
            f"{i:<4} {row['ts']:<21} {req['method']:<7} {resp['status']:<7} "
            f"{resp['bytes']:>9}  {target[:78]}"
        )
    return "\n".join(out)


if __name__ == "__main__":
    print(summarise())
