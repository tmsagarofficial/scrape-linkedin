"""Parse a LinkedIn profile URL into a public id.

Kept separate from the HTTP layer because it is pure, and because the 400 branch
of the error taxonomy (AGENTS.md §5) depends entirely on getting it right: a URL
that is well-formed but points at a company, a job or a post must be rejected as
a client error, not attempted and then reported as a 404.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

#: A vanity slug. LinkedIn allows unicode, hyphens and digits.
_SLUG_RE = re.compile(r"^[\w\-.%]{1,120}$", re.UNICODE)

#: Locale prefixes that may precede /in/, e.g. linkedin.com/in/... vs
#: uk.linkedin.com/in/...
_HOSTS = {"linkedin.com", "www.linkedin.com"}


class InvalidProfileURL(ValueError):
    """The input is not a LinkedIn member profile URL."""


def public_id_from_url(value: str) -> str:
    """Extract the vanity slug from a LinkedIn profile URL.

    Accepts a bare slug, a full URL with or without scheme, regional subdomains,
    trailing slashes, and query strings.

    Raises :class:`InvalidProfileURL` for anything that is not a *member*
    profile — company pages, schools, jobs and posts included.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidProfileURL("no URL supplied")

    raw = value.strip()

    # A bare slug is a legitimate input to /v1/profile/{public_id}.
    if "/" not in raw and "." not in raw:
        if _SLUG_RE.match(raw):
            return unquote(raw)
        raise InvalidProfileURL(f"not a valid profile slug: {raw!r}")

    candidate = raw if "//" in raw else f"https://{raw}"
    parsed = urlparse(candidate)

    host = (parsed.netloc or "").lower().split(":")[0]
    # Allow regional subdomains such as uk.linkedin.com or in.linkedin.com.
    if host and not (host in _HOSTS or host.endswith(".linkedin.com")):
        raise InvalidProfileURL(f"not a linkedin.com URL: {host}")

    segments = [s for s in parsed.path.split("/") if s]
    if "in" not in segments:
        kind = segments[0] if segments else "unknown"
        raise InvalidProfileURL(
            f"not a member profile URL (looks like a '{kind}' page); "
            "expected linkedin.com/in/<public-id>"
        )

    index = segments.index("in")
    if index + 1 >= len(segments):
        raise InvalidProfileURL("profile URL has no public id after /in/")

    slug = unquote(segments[index + 1])
    if not _SLUG_RE.match(segments[index + 1]):
        raise InvalidProfileURL(f"not a valid profile slug: {slug!r}")
    return slug
