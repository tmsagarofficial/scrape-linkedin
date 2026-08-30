"""A hard daily ceiling on live fetches made with the *shared* session.

The demo API key is published in the README on purpose, so anyone who reads it
can call this API. Combined with a shared LinkedIn session that is a real
hazard: the requests are attributed to one personal account, and sustained
automated traffic is exactly the pattern LinkedIn restricts accounts for.

A per-minute rate limit does not solve this. Twenty requests a minute is
polite-looking and still 28,800 a day — orders of magnitude beyond what a person
browsing LinkedIn produces. The limit that matters is a **daily total**, low
enough that the traffic stays within the range a human plausibly generates.

Three ways out of the ceiling, in the order they should be preferred:

1. Cached profiles keep working. They cost LinkedIn nothing.
2. A caller supplies their own session (``X-LI-AT``), which does not touch this
   budget — their credential, their risk, their account.
3. Run it locally with your own cookies.

The budget is stored in SQLite so it survives a restart. Otherwise a crash loop
would reset it and the ceiling would mean nothing.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_fetch_budget (
    day    TEXT PRIMARY KEY,
    used   INTEGER NOT NULL DEFAULT 0
);
"""


class DailyBudget:
    """Counts live profile fetches made with the shared session, per UTC day."""

    def __init__(self, path: str, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def used(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT used FROM live_fetch_budget WHERE day = ?", (self._today(),)
            ).fetchone()
        return row[0] if row else 0

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def consume(self) -> bool:
        """Record one live fetch. Returns False if the ceiling is already hit.

        Checked and incremented under one lock so concurrent requests cannot
        both pass a check that only one of them should.
        """
        with self._lock:
            today = self._today()
            row = self._conn.execute(
                "SELECT used FROM live_fetch_budget WHERE day = ?", (today,)
            ).fetchone()
            used = row[0] if row else 0
            if used >= self.limit:
                return False
            self._conn.execute(
                "INSERT INTO live_fetch_budget (day, used) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET used = used + 1",
                (today,),
            )
            self._conn.commit()
            return True

    def stats(self) -> dict[str, Any]:
        used = self.used()
        return {
            "limit_per_day": self.limit,
            "used_today": used,
            "remaining_today": max(0, self.limit - used),
            "applies_to": "live fetches using the shared session",
            "exempt": "requests supplying their own session via X-LI-AT",
        }
