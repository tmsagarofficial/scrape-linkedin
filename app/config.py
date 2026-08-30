"""Runtime configuration, read from the environment.

Every secret is supplied by an environment variable and none has a real default,
so a misconfigured deployment fails visibly rather than falling back to a value
baked into the source. `.env.example` lists them all with empty values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Project root, for locating a .env file next to pyproject.toml.
_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = _ROOT / ".env") -> None:
    """Load KEY=VALUE pairs from .env into the environment, if present.

    The README tells a reader to `cp .env.example .env` and then run the app.
    Without this that instruction silently does nothing: the process reads only
    the real environment, so a correctly-filled .env produces a 503 and no
    explanation. Documented behaviour has to be actual behaviour.

    Real environment variables always win, so a deployment that injects secrets
    directly is never overridden by a stray file. Parsing is deliberately
    minimal — enough for `KEY=value`, `export KEY=value`, comments and simple
    quoting — rather than taking a dependency for it.
    """
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Never clobber a value the environment already supplies.
        os.environ.setdefault(key, value)


_load_dotenv()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class Settings:
    """Process configuration. Instantiated once at import."""

    #: Session cookie. In practice the primary auth path — see AGENTS.md §3:
    #: LinkedIn's login flow issues a JavaScript challenge that has no headless
    #: workaround, so an LI_AT override is the known-correct answer, not a
    #: shortcut.
    li_at: str = field(default_factory=lambda: os.environ.get("LI_AT", ""))
    jsessionid: str = field(default_factory=lambda: os.environ.get("JSESSIONID", ""))

    #: Credentials for the login flow, attempted only if li_at is absent.
    li_user: str = field(default_factory=lambda: os.environ.get("LI_USER", ""))
    li_pass: str = field(default_factory=lambda: os.environ.get("LI_PASS", ""))

    #: Residential proxy for LinkedIn traffic only. The API itself serves direct
    #: (AGENTS.md §11) — routing our own responses through it would add latency
    #: and cost for no benefit.
    proxy_url: str = field(default_factory=lambda: os.environ.get("PROXY_URL", ""))

    #: Demo key. Documented in the README deliberately (§5): a reviewer who hits
    #: 401 with no key may simply abandon the demo.
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", "demo-key"))

    #: Path to a JSON (or gzip+base64) cache seed, imported at startup when the
    #: cache is empty. For hosts that build from git and cannot see local files.
    seed_file: str = field(
        default_factory=lambda: os.environ.get("SEED_CACHE_FILE", "")
    )

    cache_path: str = field(
        default_factory=lambda: os.environ.get("CACHE_PATH", "cache.db")
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: _int_env("CACHE_TTL_SECONDS", 86_400)
    )
    #: Outbound burst limit.
    #:
    #: Lowered from 6 to 3 after measurement. This project made 300+ requests
    #: across several days without incident, then tripped a session-wide block
    #: inside six minutes. The binding constraint is **burst rate**, not daily
    #: total, so the per-minute limit does more work than first assumed.
    rate_limit_per_min: int = field(
        default_factory=lambda: _int_env("RATE_LIMIT_PER_MIN", 3)
    )

    #: Hard daily ceiling on live profile fetches using the *shared* session.
    #:
    #: The published demo key means anyone can call this API. A per-minute limit
    #: still permits tens of thousands of requests a day, which is nothing like
    #: human browsing and is what gets an account restricted. Each profile costs
    #: roughly six upstream requests, so 20 profiles is ~120 requests/day —
    #: within the range a person plausibly generates. Halved from 40 after a
    #: soft block was observed in practice.
    #:
    #: Callers supplying their own session via X-LI-AT are exempt.
    daily_live_fetch_budget: int = field(
        default_factory=lambda: _int_env("DAILY_LIVE_FETCH_BUDGET", 20)
    )

    #: TLS impersonation profile. AGENTS.md §2.1: plain requests/httpx present a
    #: JA3 fingerprint no browser produces and are answered with HTTP 999.
    impersonate: str = field(
        default_factory=lambda: os.environ.get("IMPERSONATE", "chrome142")
    )

    @property
    def has_session(self) -> bool:
        return bool(self.li_at and self.jsessionid)

    @property
    def csrf_token(self) -> str:
        """The csrf-token header value: JSESSIONID with quotes stripped."""
        return self.jsessionid.strip('"')


settings = Settings()
