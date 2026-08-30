"""Recover structured fields from LinkedIn's pre-rendered display strings.

The SDUI/RSC layer does not expose typed values. Where the legacy Voyager API
returned ``{"startDate": {"month": 8, "year": 2022}}``, the RSC payload contains
only the string the server already formatted for display::

    "Aug 2022 - Jul 2025 · 3 yrs"

Everything in this module is therefore a lossy reconstruction of data LinkedIn
used to hand over directly. Consumers must be able to tell the difference, which
is why the mapper tags these fields as ``parsed`` in ``_meta.parse_confidence``.

Separator characters, established from a real capture
-----------------------------------------------------
These were verified byte-by-byte against a captured response, not assumed:

============================  ==============  ===============================
String                        Separator       Codepoint
============================  ==============  ===============================
``Aug 2022 - Jul 2025``       hyphen-minus    U+002D  (experience ranges)
``2018 – 2022``               en dash         U+2013  (education ranges)
``Full-time · 4 yrs 1 mo``    middle dot      U+00B7  (field separator)
============================  ==============  ===============================

Experience and education do **not** use the same dash. Matching only the en dash
parses education correctly while silently dropping every experience date — a
failure that leaves the schema populated and plausible, so no smoke test catches
it. Both forms are matched here.

Note also that a degree string carries its own hyphen
(``"Bachelor of Engineering - BE, Electrical..."``), so a range split must be
anchored on surrounding whitespace rather than on the bare character.

Locale
------
These patterns assume English month abbreviations and English-language type
labels, as served to a session sending ``accept-language: en-GB,en-US``. A
profile whose ``defaultLocale`` differs (AGENTS.md §2.6) will yield display
strings these patterns do not match. Every function degrades to a partial or
empty result and logs a miss rather than raising, so a locale mismatch costs
individual fields, never the request.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

#: U+00B7 MIDDLE DOT, the separator between fields in a single display string.
MIDDLE_DOT = "·"

#: Both dash forms LinkedIn uses in date ranges, plus the em dash for safety.
_DASHES = "-–—"

#: A range split on a dash that is surrounded by whitespace. The whitespace
#: anchor is what keeps "Bachelor of Engineering - BE" from being torn in half.
_RANGE_RE = re.compile(rf"^(.*?)\s+[{_DASHES}]\s+(.*)$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: "Aug 2025", "August 2025", or a bare "2018".
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")
#: "February 26, 2025" — used in recommendation relationship lines. The day is
#: captured but discarded: the schema records month precision only.
_FULL_DATE_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

#: "4 yrs 1 mo", "3 yrs", "3 mos", "1 yr 1 mo".
_YEARS_RE = re.compile(r"(\d+)\s*yr", re.I)
_MONTHS_RE = re.compile(r"(\d+)\s*mo", re.I)

#: Employment types, as LinkedIn labels them, mapped to schema values.
EMPLOYMENT_TYPES = {
    "full-time": "full_time",
    "part-time": "part_time",
    "self-employed": "self_employed",
    "freelance": "freelance",
    "contract": "contract",
    "internship": "internship",
    "apprenticeship": "apprenticeship",
    "seasonal": "seasonal",
}

#: Work arrangement labels, mapped to schema values.
WORK_MODES = {
    "on-site": "on_site",
    "onsite": "on_site",
    "hybrid": "hybrid",
    "remote": "remote",
}

#: Markers that an end date is open. English-only, by the locale caveat above.
_PRESENT = {"present", "current", "now"}


def parse_duration_string(value: str | None) -> dict[str, Any]:
    """Split a string such as ``"Full-time · 4 yrs 1 mo"`` into its parts.

    Both halves are optional: a string may carry only an employment type
    (``"Internship"``), only a duration (``"3 mos"``), or a company name
    followed by a type (``"Ravenn · Internship"``).

    Returns a dict with ``employment_type``, ``years``, ``months``,
    ``total_months`` and ``text``; unrecognised parts are ``None``.
    """
    result: dict[str, Any] = {
        "employment_type": None,
        "years": None,
        "months": None,
        "total_months": None,
        "text": value,
    }
    if not isinstance(value, str) or not value.strip():
        return result

    for part in (p.strip() for p in value.split(MIDDLE_DOT)):
        if not part:
            continue

        mapped = EMPLOYMENT_TYPES.get(part.lower())
        if mapped:
            result["employment_type"] = mapped
            continue

        years = _YEARS_RE.search(part)
        months = _MONTHS_RE.search(part)
        if years or months:
            result["years"] = int(years.group(1)) if years else 0
            result["months"] = int(months.group(1)) if months else 0
            result["total_months"] = result["years"] * 12 + result["months"]

    if result["employment_type"] is None and result["total_months"] is None:
        log.debug("no duration or employment type recognised in %r", value)

    return result


def parse_date_range_string(value: str | None) -> dict[str, Any]:
    """Parse a rendered date range into structured endpoints.

    Handles every form observed in a real capture::

        "2018 – 2022"                    en dash, year only
        "Aug 2022 - Jul 2025 · 3 yrs"    hyphen, month precision, + duration
        "Aug 2025 - Present · 1 yr 1 mo" open-ended
        "Aug 2020"                       a single point in time

    ``month`` is ``None`` whenever LinkedIn rendered a year alone — per AGENTS.md
    §6, partial dates are normal and must never be filled in with a guess.

    Returns ``start``, ``end``, ``is_current``, ``duration_months`` and ``text``.
    """
    result: dict[str, Any] = {
        "start": None,
        "end": None,
        "is_current": False,
        "duration_months": None,
        "text": value,
    }
    if not isinstance(value, str) or not value.strip():
        return result

    # A trailing "· 3 yrs" duration is carried alongside the range itself.
    head, _, tail = value.partition(MIDDLE_DOT)
    if tail.strip():
        duration = parse_duration_string(tail.strip())
        result["duration_months"] = duration["total_months"]

    head = head.strip()
    match = _RANGE_RE.match(head)
    if match:
        start_text, end_text = match.group(1).strip(), match.group(2).strip()
    else:
        # No dash: a single date, which we treat as the start.
        start_text, end_text = head, ""

    result["start"] = _parse_point(start_text)

    if end_text.lower() in _PRESENT:
        result["is_current"] = True
    elif end_text:
        result["end"] = _parse_point(end_text)

    if result["start"] is None and result["end"] is None:
        log.debug("no dates recognised in %r", value)

    return result


def parse_location_string(value: str | None) -> dict[str, Any]:
    """Split ``"Hyderabad, Telangana, India · On-site"`` into its parts.

    The same ``X · Y`` shape also carries employment type
    (``"Ravenn · Internship"``), so the trailing segment is only treated as a
    work mode when it matches a known one; otherwise it is left in the location
    rather than being discarded.

    Returns ``location``, ``work_mode`` and ``text``.
    """
    result: dict[str, Any] = {"location": None, "work_mode": None, "text": value}
    if not isinstance(value, str) or not value.strip():
        return result

    parts = [p.strip() for p in value.split(MIDDLE_DOT) if p.strip()]
    if not parts:
        return result

    # Only strip a trailing segment we positively recognise.
    if len(parts) > 1 and parts[-1].lower() in WORK_MODES:
        result["work_mode"] = WORK_MODES[parts[-1].lower()]
        parts = parts[:-1]
    elif len(parts) == 1 and parts[0].lower() in WORK_MODES:
        result["work_mode"] = WORK_MODES[parts[0].lower()]
        parts = []

    if parts:
        result["location"] = f" {MIDDLE_DOT} ".join(parts)

    return result


def _parse_point(text: str) -> dict[str, int | None] | None:
    """Parse one endpoint of a range into ``{"year": int, "month": int|None}``."""
    text = text.strip()
    if not text:
        return None

    match = _FULL_DATE_RE.match(text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        return {"year": int(match.group(3)), "month": month}

    match = _MONTH_YEAR_RE.match(text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        if month is not None:
            return {"year": int(match.group(2)), "month": month}
        # A four-digit year with an unrecognised (likely non-English) month:
        # keep the year rather than discarding the whole endpoint.
        log.debug("unrecognised month name %r", match.group(1))
        return {"year": int(match.group(2)), "month": None}

    match = _YEAR_RE.match(text)
    if match:
        return {"year": int(match.group(1)), "month": None}

    log.debug("unrecognised date endpoint %r", text)
    return None
