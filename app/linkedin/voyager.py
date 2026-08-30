"""Voyager Dash identity record: `GET /voyager/api/identity/dash/profiles`.

This endpoint was missed during the original reverse engineering, and the way it
was missed is worth stating: the method was traffic capture, and the flagship web
client **never calls it**. It appears in none of eight browser HARs. Three other
independent implementations of the same brief found it, which is how it came to
light here.

It is not a replacement for the SDUI path, and the difference is not a matter of
preference:

* It returns the **identity record only**. Experience and education are present
  as ``fsd_profileCard`` URN *pointers*, not as data.
* Everything it does return is **typed**, and several fields have no equivalent
  anywhere in the SDUI payload — locale variants, image expiry, industry,
  websites, and an authoritative first/last name split.

So the two are complementary. Identity comes from here because the data is
better; sections come from SDUI because that is the only place they exist.

Two further properties, both established by measurement rather than assumption:

* **It answers a plain HTTP client.** Requested with `requests` and with
  `curl_cffi` Chrome impersonation, it returned byte-identical 200s. TLS
  impersonation is not load-bearing for this endpoint.
* **A wrong ``decorationId`` produces a redirect loop, not an error.** LinkedIn
  302s the request to its own URL rather than returning 4xx, which a client will
  happily follow thirty times. See `MAX_REDIRECTS` in `client.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.schemas import Image, Name, Website

log = logging.getLogger(__name__)

DASH_PROFILES = (
    "https://www.linkedin.com/voyager/api/identity/dash/profiles"
    "?q=memberIdentity&memberIdentity={public_id}"
)


def _first(value: Any, *keys: str) -> Any:
    """Walk a chain of dict keys, returning None the moment one is missing."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _expiry_from(url_segment: str | None) -> str | None:
    """Extract the `e=` expiry from a signed media path and format it ISO-8601.

    LinkedIn embeds expiry in the query string as seconds since the epoch::

        .../0/1598359268633?e=1789603200&v=beta&t=OG5sXhET...
    """
    if not url_segment or "e=" not in url_segment:
        return None
    try:
        raw = url_segment.split("e=", 1)[1].split("&", 1)[0]
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _image(picture: Any) -> Image | None:
    """Build an Image from a Voyager vectorImage, largest rendition first.

    The largest is chosen as the primary URL because a consumer that wants a
    thumbnail can pick one from ``sizes``, whereas one that wants full
    resolution cannot invent it.
    """
    vector = _first(picture, "displayImage", "vectorImage")
    if not isinstance(vector, dict):
        return None
    root = vector.get("rootUrl")
    artifacts = vector.get("artifacts") or []
    if not root or not artifacts:
        return None

    usable = [
        a for a in artifacts
        if isinstance(a, dict) and a.get("fileIdentifyingUrlPathSegment")
    ]
    if not usable:
        return None
    usable.sort(key=lambda a: a.get("width") or 0)

    largest = usable[-1]
    segment = largest["fileIdentifyingUrlPathSegment"]
    return Image(
        url=f"{root}{segment}",
        width=largest.get("width"),
        expires_at=_expiry_from(segment),
        sizes=[
            {
                "width": a.get("width"),
                "height": a.get("height"),
                "url": f"{root}{a['fileIdentifyingUrlPathSegment']}",
            }
            for a in usable
        ],
    )


def _locales(value: Any) -> dict[str, str]:
    """Keep only the string entries of a multiLocale map."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, str) and v}


def _locale_tag(primary: Any) -> str | None:
    """Render a Voyager locale object as `en_US`."""
    if not isinstance(primary, dict):
        return None
    language, country = primary.get("language"), primary.get("country")
    if not language:
        return None
    return f"{language}_{country}" if country else language


def parse_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a Dash identity response into public-schema fragments.

    Returns only the keys it can actually fill, so a caller can merge without
    overwriting good SDUI values with `None`. Never raises: a shape change
    should cost individual fields, not the request.
    """
    included = payload.get("included") or []
    record = next(
        (
            item for item in included
            if isinstance(item, dict)
            and str(item.get("$type", "")).endswith("identity.profile.Profile")
        ),
        None,
    )
    if record is None:
        log.debug("no Profile entity in the Dash identity response")
        return {}

    out: dict[str, Any] = {}

    first, last = record.get("firstName"), record.get("lastName")
    if first or last:
        full = " ".join(part for part in (first, last) if part)
        out["name"] = Name(
            first=first, last=last, full=full,
            locales=_locales(record.get("multiLocaleFirstName")),
            split_inferred=False,
        )

    for source, target in (
        ("headline", "headline"),
        ("summary", "about"),
        ("publicIdentifier", "public_id"),
    ):
        value = record.get(source)
        if isinstance(value, str) and value.strip():
            out[target] = value

    headline_locales = _locales(record.get("multiLocaleHeadline"))
    if headline_locales:
        out["headline_locales"] = headline_locales
    about_locales = _locales(record.get("multiLocaleSummary"))
    if about_locales:
        out["about_locales"] = about_locales

    locale = _locale_tag(record.get("primaryLocale"))
    if locale:
        out["primary_locale"] = locale

    images: dict[str, Image] = {}
    for key, name in (("profilePicture", "profile"), ("backgroundPicture", "background")):
        image = _image(record.get(key))
        if image:
            images[name] = image
    if images:
        out["images"] = images

    websites = [
        Website(url=w["url"], category=w.get("category"))
        for w in (record.get("websites") or [])
        if isinstance(w, dict) and w.get("url")
    ]
    if websites:
        out["websites"] = websites

    country = _first(record.get("location"), "countryCode")
    if country:
        out["country_code"] = country

    for source, target in (("premium", "is_premium"), ("showVerificationBadge", "is_verified")):
        if isinstance(record.get(source), bool):
            out[target] = record[source]

    # Retained for diagnostics: these are the pointers that make clear the
    # identity record does not carry section content.
    out["_card_urns"] = {
        key: record[key]
        for key in ("experienceCardUrn", "educationCardUrn")
        if record.get(key)
    }
    return out
