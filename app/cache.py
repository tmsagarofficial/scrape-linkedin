"""SQLite profile cache.

AGENTS.md §7 sets the design assumption plainly: **the session will be dead when
a reviewer opens this.** The cache is what keeps the API useful in that state, so
it is not an optimisation — it is the primary availability mechanism.

Two behaviours follow from that:

* **stale-if-error, at any age.** A normal request honours the TTL. A request
  that fails upstream falls back to whatever is stored regardless of how old it
  is, and says so in ``_meta``. A stale answer beats a 503.
* **WAL mode.** Reads do not block on the writer, so a slow upstream fetch never
  stalls a cache-served request.

Stored payloads are whole serialised responses. They are profile data, so the
database file is gitignored (§9) and never committed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    public_id   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_fetched_at ON profiles (fetched_at);
"""


class ProfileCache:
    """A small write-through cache keyed by public id."""

    def __init__(self, path: str, ttl_seconds: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, public_id: str, *, allow_stale: bool = False) -> tuple[dict, int] | None:
        """Return ``(payload, age_seconds)``, or None.

        With ``allow_stale`` the TTL is ignored — the stale-if-error path.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM profiles WHERE public_id = ?",
                (public_id,),
            ).fetchone()

        if row is None:
            return None

        payload, fetched_at = row
        age = int(time.time() - fetched_at)
        if not allow_stale and age > self.ttl_seconds:
            return None

        try:
            return json.loads(payload), age
        except ValueError:
            log.warning("cache entry for %s is corrupt; ignoring", public_id)
            return None

    def put(self, public_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO profiles (public_id, payload, fetched_at) "
                "VALUES (?, ?, ?) ON CONFLICT(public_id) DO UPDATE SET "
                "payload = excluded.payload, fetched_at = excluded.fetched_at",
                (public_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def entries(self) -> list[dict[str, Any]]:
        """List what is cached, without returning the profile bodies.

        Supports the transparency endpoint: a person can see *that* they are
        cached without the listing itself handing out everyone's data.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT public_id, payload, fetched_at FROM profiles "
                "ORDER BY fetched_at DESC"
            ).fetchall()

        now = time.time()
        out = []
        for public_id, payload, fetched_at in rows:
            seeded_at = None
            try:
                seeded_at = json.loads(payload).get("_meta", {}).get("seeded_at")
            except ValueError:
                pass
            out.append(
                {
                    "public_id": public_id,
                    "age_seconds": int(now - fetched_at),
                    "pre_seeded": seeded_at is not None,
                    "seeded_at": seeded_at,
                }
            )
        return out

    def delete(self, public_id: str) -> bool:
        """Remove one profile. Returns True if something was removed."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM profiles WHERE public_id = ?", (public_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total, oldest, newest = self._conn.execute(
                "SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM profiles"
            ).fetchone()
        now = time.time()
        return {
            "entries": total or 0,
            "oldest_age_seconds": int(now - oldest) if oldest else None,
            "newest_age_seconds": int(now - newest) if newest else None,
            "ttl_seconds": self.ttl_seconds,
        }

    def load_seed(self, path: str) -> int:
        """Import a JSON seed exported by scripts/export_seed.py.

        Hosts that build from git cannot see a local seed file, so the seed
        arrives as dashboard-managed secret file content instead. Accepts either
        plain JSON or gzip+base64, which is roughly a quarter the size and the
        difference between a comfortable paste and an awkward one.

        Existing entries are never overwritten: a live-fetched profile is always
        fresher than a seed.
        """
        import base64
        import gzip

        source = Path(path)
        if not source.exists():
            return 0

        try:
            text = source.read_text().strip()
            if not text.startswith("{"):
                text = gzip.decompress(base64.b64decode(text)).decode()
            seed = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - a bad seed must not stop boot
            log.warning("could not read seed %s: %s", path, exc)
            return 0

        loaded = 0
        for public_id, payload in seed.items():
            if self.get(public_id, allow_stale=True) is None:
                self.put(public_id, payload)
                loaded += 1
        return loaded

    def close(self) -> None:
        with self._lock:
            self._conn.close()
