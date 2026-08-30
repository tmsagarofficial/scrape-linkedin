"""Public response schema.

Design rule from AGENTS.md §6: decouple from LinkedIn's internals and never leak
raw URNs. Nothing in here mirrors an SDUI component name or a company URN; the
mapper translates into these types and LinkedIn's own vocabulary stops there.

Two additions beyond §6, per the RSC amendment:

``_meta.data_layer``
    Which upstream layer produced the values, so a consumer can tell RSC-derived
    output from anything sourced differently later.

``_meta.parse_confidence``
    Per-field provenance. The SDUI layer serves pre-rendered display strings, so
    ``duration_months`` is recovered by regex from ``"Full-time · 4 yrs 1 mo"``
    rather than read from a typed field. A consumer must be able to see that a
    value was inferred. Values are ``"raw"`` (a discrete value LinkedIn supplied
    on its own) or ``"parsed"`` (recovered from a display string).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class Provenance(str, Enum):
    """How a field's value was obtained."""

    RAW = "raw"
    PARSED = "parsed"


class PartialDate(BaseModel):
    """A date LinkedIn may only have to year precision.

    §6: dates are partial by nature and ``month`` must be nullable. A missing
    month is recorded as ``None`` and never inferred.
    """

    year: int
    month: int | None = Field(default=None, ge=1, le=12)


class Name(BaseModel):
    """A member's name.

    ``first`` and ``last`` come from LinkedIn directly when the Voyager identity
    record is available. Splitting ``full`` on whitespace is a poor substitute —
    it mangles multi-word surnames, patronymics and mononyms — so the split is
    only inferred when nothing authoritative was returned.
    """

    first: str | None = None
    last: str | None = None
    full: str
    #: Locale variants, e.g. {"ru_RU": "Алексе́й", "en_US": "Alexey"}.
    locales: dict[str, str] = Field(default_factory=dict)
    #: True when first/last were inferred by splitting rather than supplied.
    split_inferred: bool = False


class Location(BaseModel):
    raw: str
    country_code: str | None = None


class Image(BaseModel):
    """A LinkedIn-hosted image.

    Image URLs are signed and expire. ``expires_at`` is surfaced so a consumer
    does not mistake one for a permanent link and cache it past its life.

    ``sizes`` carries the other renditions LinkedIn offers for the same image,
    smallest first, so a caller can pick a width instead of re-requesting.
    """

    url: str
    width: int | None = None
    expires_at: str | None = None
    sizes: list[dict[str, Any]] = Field(default_factory=list)


class Website(BaseModel):
    url: str
    category: str | None = None


class Company(BaseModel):
    """An employer.

    ``public_id`` is the numeric entity id the SDUI layer exposes rather than a
    vanity slug. That is not a limitation in practice: LinkedIn redirects
    ``/company/1586/`` to ``/company/amazon/``, so ``url`` is a stable, working
    link and is what consumers should follow.
    """

    name: str
    public_id: str | None = None
    url: str | None = None
    logo: str | None = None


class Role(BaseModel):
    """One position. Several may share an employer under a grouped entry."""

    title: str
    employment_type: str | None = None
    location: str | None = None
    work_mode: str | None = None
    start: PartialDate | None = None
    end: PartialDate | None = None
    is_current: bool = False
    duration_months: int | None = None
    description: str | None = None


class Experience(Role):
    company: Company


class Education(BaseModel):
    school: str
    school_public_id: str | None = None
    school_url: str | None = None
    degree: str | None = None
    field: str | None = None
    start: PartialDate | None = None
    end: PartialDate | None = None


class Skill(BaseModel):
    name: str
    endorsement_count: int | None = None
    #: Certifications LinkedIn renders beneath the skill as evidence for it.
    credentials: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    name: str
    authority: str | None = None
    issued: PartialDate | None = None
    credential_id: str | None = None
    url: str | None = None


class Language(BaseModel):
    name: str
    proficiency: str | None = None


class Course(BaseModel):
    name: str


class Recommendation(BaseModel):
    """A single recommendation written by, or about, this member."""

    author: str
    author_headline: str | None = None
    #: Connection distance as rendered, e.g. "3rd+".
    author_degree: str | None = None
    #: The relationship line, e.g. "February 26, 2025, Ben was Mike's client".
    context: str | None = None
    date: PartialDate | None = None
    text: str | None = None


class Recommendations(BaseModel):
    """Recommendations, split by direction.

    The counts come from the card's tab headers ("Received (2)"). The bodies are
    emitted under a *separate, unreferenced* record in the same response, which
    is why they are only available once the parser walks every root rather than
    just record ``0``.
    """

    received_count: int | None = None
    given_count: int | None = None
    received: list[Recommendation] = Field(default_factory=list)
    given: list[Recommendation] = Field(default_factory=list)


class Honor(BaseModel):
    """An award or honour."""

    title: str
    issuer: str | None = None
    issued: PartialDate | None = None
    associated_with: str | None = None
    description: str | None = None


class SectionCoverage(BaseModel):
    """What a section returned, and whether it was complete.

    ``truncated`` distinguishes the two cases §2.7 and §12 require keeping
    apart: a section that is genuinely empty, and one that returned only its
    first few entries. ``total`` is populated only when LinkedIn stated it
    outright (``"Show all 6 licenses"``); a bare ``"Show all"`` leaves it None
    rather than guessing.
    """

    returned: int = 0
    total: int | None = None
    truncated: bool = False
    fetched: bool = True


class Meta(BaseModel):
    source: Literal["live", "cache", "partial"] = "live"
    data_layer: Literal["rsc_parsed"] = "rsc_parsed"
    fetched_at: str
    cache_age_seconds: int = 0
    endpoint_used: str
    components_used: list[str] = Field(default_factory=list)
    #: True when full lists were fetched instead of the truncated cards.
    complete: bool = False
    completeness: dict[str, float] = Field(default_factory=dict)
    coverage: dict[str, SectionCoverage] = Field(default_factory=dict)
    parse_confidence: dict[str, Provenance] = Field(default_factory=dict)
    partial_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    public_id: str
    #: Canonical profile URL, built from the public id.
    profile_url: str
    #: LinkedIn's durable member id (``ACoAA…``). Stable across vanity renames,
    #: which the public id is not.
    profile_id: str | None = None
    profile_urn: str | None = None
    name: Name
    headline: str | None = None
    #: Locale variants of the headline, where LinkedIn provides them.
    headline_locales: dict[str, str] = Field(default_factory=dict)
    pronouns: str | None = None
    location: Location | None = None
    about: str | None = None
    #: Locale variants of the About text.
    about_locales: dict[str, str] = Field(default_factory=dict)
    #: The locale the profile was authored in. A profile has a primary locale
    #: that sticks regardless of the viewer's language setting, so the same
    #: profile can return content in a language the caller did not ask for.
    primary_locale: str | None = None
    industry: str | None = None
    websites: list[Website] = Field(default_factory=list)
    is_premium: bool | None = None
    is_verified: bool | None = None
    follower_count: int | None = None
    connection_count: str | None = None
    images: dict[str, Image] = Field(default_factory=dict)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    volunteer_causes: list[str] = Field(default_factory=list)
    recommendations: Recommendations | None = None


class ProfileResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    profile: Profile
    meta: Meta = Field(serialization_alias="_meta")

    model_config = {"populate_by_name": True}
