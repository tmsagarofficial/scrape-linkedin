#!/usr/bin/env python3
"""Fail if credentials or scraped personal data are committed.

AGENTS.md §0 makes "zero credentials or secrets in the repository or git
history" a hard constraint, and §9 forbids committing harvested profile bodies
to a public repo. Both are checked here so the answer is enforced rather than
remembered.

    python3 scripts/scan_secrets.py             # working tree
    python3 scripts/scan_secrets.py --history   # every commit as well

Exits non-zero on the first finding.
"""

from __future__ import annotations

import re
import subprocess
from fnmatch import fnmatch
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: (label, pattern). Written to match the *shape* of a secret, so that a real
#: value is caught even though no real value appears in this file.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # A LinkedIn session cookie: long opaque base64-ish blob starting AQED.
    ("li_at session cookie", re.compile(r"\bAQED[A-Za-z0-9_\-]{40,}")),
    # JSESSIONID always carries an "ajax:" prefix and a long numeric id.
    ("JSESSIONID value", re.compile(r"ajax:\d{12,}")),
    # A proxy URL with inline credentials.
    ("proxy credentials", re.compile(r"https?://[^\s:/@]+:[^\s:/@]+@[^\s/]+")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

#: Committed files that legitimately describe secret *shapes*.
ALLOWLIST = {"scripts/scan_secrets.py", ".env.example"}

#: Credential-shaped strings that are documentation, not secrets. Documenting
#: the format of PROXY_URL requires writing something that looks exactly like a
#: proxy URL; flagging it would train a reader to ignore this scanner's output,
#: which is worse than the finding.
PLACEHOLDERS = re.compile(
    r"(?:user|username|USER|pass|password|PASS|host|HOST|port|PORT"
    r"|xxx+|placeholder|example|changeme|<[^>]+>)",
)

#: Never committed; enforced separately from content matching because these are
#: dangerous by existence, not by what is inside them.
FORBIDDEN_PATHS = (".env", "cache.db", "profileView_raw.json")
FORBIDDEN_SUFFIXES = (".har", ".pem", ".db", ".sqlite3")
FORBIDDEN_DIRS = ("docs/evidence/raw/",)


def tracked_files() -> list[str]:
    """Files git is tracking, or every file if this is not a repository yet.

    Falling back matters: an empty list would make the scan pass vacuously,
    which is the worst possible outcome for a check whose whole job is to fail.
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False
    )
    files = [line for line in out.stdout.splitlines() if line.strip()]
    if files:
        return files

    print("note: no git repository; scanning the working tree instead")
    ignored = _gitignore_patterns()
    skip = {"venv", ".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}
    files = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if skip & set(rel.parts) or _is_ignored(str(rel), ignored):
            continue
        files.append(str(rel))
    return files


def _gitignore_patterns() -> list[str]:
    """Read .gitignore so the fallback scan matches what git would track.

    Without this the fallback flags every gitignored artefact as "committed",
    which is both wrong and noisy enough to train someone to ignore the output.
    """
    path = REPO / ".gitignore"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _is_ignored(rel: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        if fnmatch(rel, pattern) or fnmatch(Path(rel).name, pattern):
            return True
        # A directory pattern ignores everything beneath it.
        if rel.startswith(f"{pattern}/"):
            return True
    return False


def scan_text(label: str, text: str) -> list[str]:
    findings = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if PLACEHOLDERS.search(value):
                continue
            findings.append(f"{label}: {name} (starts {value[:12]!r})")
            break
    return findings


def scan_working_tree() -> list[str]:
    findings: list[str] = []
    for rel in tracked_files():
        if rel in ALLOWLIST:
            continue

        if (
            rel in FORBIDDEN_PATHS
            or rel.endswith(FORBIDDEN_SUFFIXES)
            or any(rel.startswith(d) for d in FORBIDDEN_DIRS)
        ):
            findings.append(f"{rel}: file must never be committed")
            continue

        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(rel, text))
    return findings


def scan_history() -> list[str]:
    """Scan every commit. A secret deleted later is still in the history."""
    out = subprocess.run(
        ["git", "log", "-p", "--no-color"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    findings = []
    for name, pattern in PATTERNS:
        if pattern.search(out.stdout):
            findings.append(f"git history: {name}")
    return findings


def main() -> int:
    findings = scan_working_tree()
    if "--history" in sys.argv:
        findings += scan_history()

    if findings:
        print("SECRET SCAN FAILED\n")
        for finding in findings:
            print(f"  - {finding}")
        print(
            "\nRemove the value and rotate it. For history, the file must be "
            "purged (git filter-repo) — deleting it in a later commit is not "
            "sufficient."
        )
        return 1

    print("Secret scan clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
