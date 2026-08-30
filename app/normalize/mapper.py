"""Map parsed RSC text nodes into the public schema.

The SDUI layer gives us an ordered stream of rendered strings, each tagged with
the section that rendered it (``profile-card-experience``) and, where one
exists, the entity it links to (``/company/1586/``). This module turns that into
:class:`app.schemas.ProfileResponse`.

Segmenting experience
---------------------
Entries are grouped by ``entity_url``, because every string belonging to one
employer shares that employer's link. Within a group, roles are delimited by
**date-range strings**, not by a fixed number of lines.

That choice is deliberate. An earlier plan proposed treating each run of three
strings as one entry; real data breaks it immediately, because LinkedIn renders
two different shapes:

*Grouped* — several roles at one employer, with employment type and location
hoisted to the employer level::

    Amazon                                    <- company
    Full-time · 4 yrs 1 mo                    <- type + total duration
    Hyderabad, Telangana, India · On-site     <- location + work mode
    SDE 2                                     <- role 1 title
    Aug 2025 - Present · 1 yr 1 mo            <- role 1 dates
    SDE                                       <- role 2 title
    Aug 2022 - Jul 2025 · 3 yrs               <- role 2 dates

*Single* — one role, with the company on the second line::

    ML intern                                 <- title
    Ravenn · Internship                       <- company · type
    Oct 2020 - Dec 2020 · 3 mos               <- dates

Counting date ranges separates the two: two or more means a grouped entry whose
first lines are employer-level; exactly one means a single role.

Confidence
----------
This segmentation is a heuristic validated against **one** profile. The two
shapes above are both present in it, which is what makes the rule better than
counting lines, but a profile with a shape neither covers will map incorrectly.

One ambiguity is known and unresolved: a *grouped* entry containing exactly one
role would present a single date range and be read as the *single* shape, taking
the company name as the job title. No such entry appears in the captured data —
LinkedIn appears to render a lone role in the single shape anyway, which would
make the case unreachable — but that is an inference from one profile, not a
verified rule. If it does occur, the symptom is a title that is really a company
name, with the employer taken from the line below it.
Anything the heuristic could not place is reported in ``_meta.warnings`` rather
than dropped silently, and every value recovered from a display string is marked
``parsed`` in ``_meta.parse_confidence``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from app.linkedin.rsc_parser import TextNode, visible
from app.linkedin.text_parsers import (
    MIDDLE_DOT,
    parse_date_range_string,
    parse_duration_string,
    parse_location_string,
)
from app.schemas import (
    Certification,
    Honor,
    Recommendation,
    Recommendations,
    Company,
    Course,
    Education,
    Experience,
    Language,
    Location,
    Meta,
    Name,
    PartialDate,
    Profile,
    ProfileResponse,
    Provenance,
    SectionCoverage,
    Skill,
)

log = logging.getLogger(__name__)

#: Section labels, as LinkedIn emits them, grouped by what they feed.
SECTION_EXPERIENCE = "profile-card-experience"
SECTION_EDUCATION = ("profile-card-education", "education-lockup-view")
SECTION_CERTIFICATIONS = "profile-card-licenses-and-certifications"
#: The "Show credential" control sits in its own section but belongs to the
#: certification above it, and carries the credential URL on its entity_url.
SECTION_CERT_CREDENTIAL = "license-certifications-see-license-button"
SECTION_SKILLS = "profile-card-skills"
SECTION_LANGUAGES = "profile-card-languages"
SECTION_COURSES = "profile-card-courses"
SECTION_HONORS = "profile-card-honors-and-awards"
SECTION_VOLUNTEER = "profile-card-volunteer-causes"
SECTION_RECOMMENDATIONS = (
    "profile-card-recommendations",
    "profile-recommendations-details-view",
)
SECTION_TOP_CARD = ("profile-top-card", "profile-sticky-header")
SECTION_ABOUT = "profile-card-about"

#: Card headers, which repeat the section name and carry no data.
_HEADERS = {
    "Experience", "Education", "Skills", "Languages", "Courses", "Interests",
    "Licenses & certifications", "Licenses and certifications",
    "Endorsements", "Contact info",
}

#: "Show all 6 licenses" -> 6. A bare "Show all" yields no total.
_SEE_ALL_RE = re.compile(r"^Show all(?:\s+(\d[\d,]*))?\b", re.I)

#: "Show all 4 details" expands one entry's supporting credentials; it says
#: nothing about whether the *list* is truncated. Counting it as truncation
#: marks a fully-paged section incomplete.
_SEE_DETAILS_RE = re.compile(r"^Show all\s+\d+\s+details?$", re.I)
_ENDORSEMENTS_RE = re.compile(r"^(\d[\d,]*)\s+endorsement", re.I)
_FOLLOWERS_RE = re.compile(r"^([\d,]+)\s+followers?$", re.I)
_ISSUED_RE = re.compile(r"^Issued\s+(.+)$", re.I)
_CREDENTIAL_RE = re.compile(r"^Credential ID\s+(.+)$", re.I)

#: A build version string that trails some paged responses, e.g. "0.1.51189".
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: "Skills:" introduces the skills a certification evidences. Like the
#: experience annotation below, the node after it is the list it introduces.
_SKILLS_LABEL_RE = re.compile(r"^Skills:$", re.I)

#: Empty-state copy rendered when a paged list has run out.
_EMPTY_STATE = ("Nothing to see for now",)

#: "Skills for Example Lead at EXAMPLE-ORG" — a supplementary control that
#: renders *inside* the experience card, between role entries. It carries no
#: entity_url, so leaving it in place splits an employer's roles into separate
#: groups and strands the ones after it. The node that follows it is the skill
#: list it introduces, and is dropped with it.
_SKILLS_FOR_RE = re.compile(r"^Skills for\b", re.I)
#: Distinctive token in a "see all" control's label -> the card it belongs to.
#: Ordered: the first match wins, so more specific tokens come first.
_SEE_ALL_OWNERS: tuple[tuple[str, str], ...] = (
    ("license-certification", SECTION_CERTIFICATIONS),
    ("licenses-and-certification", SECTION_CERTIFICATIONS),
    ("experience", SECTION_EXPERIENCE),
    ("education", SECTION_EDUCATION[0]),
    ("language", SECTION_LANGUAGES),
    ("skill", SECTION_SKILLS),
    ("course", SECTION_COURSES),
)

#: A verification call-to-action rendered above the name in the top card.
_VERIFICATION_RE = re.compile(r"^View .+ verifications?$", re.I)


def _int(text: str) -> int | None:
    try:
        return int(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _is_date_range(text: str) -> bool:
    """True if a string carries at least one parseable date endpoint."""
    parsed = parse_date_range_string(text)
    return parsed["start"] is not None or parsed["is_current"]


def _as_partial(point: dict | None) -> PartialDate | None:
    if not point or point.get("year") is None:
        return None
    return PartialDate(year=point["year"], month=point.get("month"))


class _Collector:
    """Accumulates nodes per section and records coverage as it goes."""

    def __init__(self) -> None:
        self.sections: dict[str, list[TextNode]] = {}
        #: Every retained node, in the order it was rendered.
        self.ordered: list[TextNode] = []
        self.coverage: dict[str, SectionCoverage] = {}
        self.warnings: list[str] = []

    def add(self, node: TextNode) -> None:
        section = node.section or "unknown"

        # "See all" controls are truncation signals, not content. They appear
        # both under a dedicated "...-see-all-..." label and inline within the
        # content card itself, so match on the text rather than the section.
        if _SEE_DETAILS_RE.match(node.text):
            return  # per-entry expander, not a list-truncation signal

        match = _SEE_ALL_RE.match(node.text)
        if match:
            target = self._nearest_content_section(section)
            entry = self.coverage.setdefault(target, SectionCoverage())
            entry.truncated = True
            entry.total = _int(match.group(1)) if match.group(1) else None
            return

        if node.text in _HEADERS:
            return

        self.sections.setdefault(section, []).append(node)
        self.ordered.append(node)

    def _nearest_content_section(self, see_all_section: str) -> str:
        """Map a "see all" control's label back to the card it belongs to.

        LinkedIn does not name these consistently with their cards
        (``license-certifications-see-all-button`` belongs to
        ``profile-card-licenses-and-certifications``), so the association is a
        lookup rather than a string-similarity guess. An unrecognised control is
        left under its own label instead of being attached to the wrong card.
        """
        for token, card in _SEE_ALL_OWNERS:
            if token in see_all_section:
                return card
        return see_all_section

    def get(self, *names: str) -> list[TextNode]:
        """Return nodes from the named sections, preserving document order.

        Order matters: concatenating section by section would interleave cards
        by argument order rather than by how they render, which is exactly the
        ordering bug AGENTS.md §2.5 warns about.
        """
        wanted = set(names)
        return [n for n in self.ordered if (n.section or "unknown") in wanted]


def _group_by_entity(nodes: Sequence[TextNode]) -> list[list[TextNode]]:
    """Split an ordered node run into consecutive groups sharing an entity_url."""
    groups: list[list[TextNode]] = []
    for node in nodes:
        if groups and groups[-1][0].entity_url == node.entity_url:
            groups[-1].append(node)
        else:
            groups.append([node])
    return groups


def _company_public_id(url: str | None) -> str | None:
    """Extract the company or school slug/id from its LinkedIn URL."""
    if not url:
        return None
    match = re.search(r"/(?:company|school)/([^/?]+)", url)
    return match.group(1) if match else None


def _drop_skill_annotations(nodes: Sequence[TextNode]) -> list[TextNode]:
    """Remove "Skills for ..." controls and the skill list each introduces."""
    kept: list[TextNode] = []
    skip_next = False
    for node in nodes:
        if skip_next:
            skip_next = False
            continue
        if _SKILLS_FOR_RE.match(node.text):
            skip_next = True
            continue
        kept.append(node)
    return kept


def _map_experience(
    nodes: Sequence[TextNode], confidence: dict[str, Provenance], warnings: list[str]
) -> list[Experience]:
    out: list[Experience] = []

    for group in _group_by_entity(_drop_skill_annotations(nodes)):
        texts = [n.text for n in group]
        public_id = _company_public_id(group[0].entity_url)
        date_positions = [i for i, t in enumerate(texts) if _is_date_range(t)]

        if not date_positions:
            warnings.append(
                f"experience entry for {public_id or 'unknown company'} had no "
                "recognisable date range and was skipped"
            )
            continue

        if len(date_positions) >= 2:
            entries = _grouped_entry(
                texts, date_positions, public_id, group[0].entity_url
            )
        else:
            entries = _single_entry(
                texts, date_positions[0], public_id, group[0].entity_url
            )

        for entry in entries:
            index = len(out)
            confidence[f"experience[{index}].start"] = Provenance.PARSED
            confidence[f"experience[{index}].end"] = Provenance.PARSED
            confidence[f"experience[{index}].duration_months"] = Provenance.PARSED
            if entry.employment_type:
                confidence[f"experience[{index}].employment_type"] = Provenance.PARSED
            out.append(entry)

    return out


def _grouped_entry(
    texts: list[str],
    date_positions: list[int],
    public_id: str | None,
    entity_url: str | None = None,
) -> list[Experience]:
    """Several roles at one employer; type and location are employer-level."""
    company_name = texts[0]
    header = texts[1 : date_positions[0] - 1]

    employment_type = None
    location = work_mode = None
    for line in header:
        duration = parse_duration_string(line)
        if duration["employment_type"]:
            employment_type = duration["employment_type"]
            continue
        place = parse_location_string(line)
        if place["work_mode"] or "," in line:
            location, work_mode = place["location"], place["work_mode"]

    entries = []
    for index, position in enumerate(date_positions):
        title = texts[position - 1] if position >= 1 else company_name
        dates = parse_date_range_string(texts[position])

        # A role may carry its own location on the line after its dates,
        # overriding the employer-level one.
        #
        # Whether that line is a location or the *next role's title* is not a
        # guess: a role's title is always the line immediately before its date
        # range. So the line after these dates is a location precisely when it
        # is not the line before the next role's dates. Inferring it from
        # punctuation instead would drop single-word locations like "Bangalore".
        role_location, role_mode = location, work_mode
        following = position + 1
        if following < len(texts):
            next_title_index = (
                date_positions[index + 1] - 1
                if index + 1 < len(date_positions)
                else None
            )
            if following != next_title_index:
                place = parse_location_string(texts[following])
                role_location = place["location"]
                role_mode = place["work_mode"]

        entries.append(
            Experience(
                title=title,
                company=Company(
                    name=company_name, public_id=public_id, url=entity_url
                ),
                employment_type=employment_type,
                location=role_location,
                work_mode=role_mode,
                start=_as_partial(dates["start"]),
                end=_as_partial(dates["end"]),
                is_current=dates["is_current"],
                duration_months=dates["duration_months"],
            )
        )
    return entries


def _single_entry(
    texts: list[str],
    date_position: int,
    public_id: str | None,
    entity_url: str | None = None,
) -> list[Experience]:
    """One role: title, then company (optionally with employment type), dates."""
    title = texts[0]
    company_line = texts[1] if date_position >= 2 else ""
    parsed = parse_duration_string(company_line)
    company_name = company_line.split("·")[0].strip() or (public_id or "")

    location = work_mode = None
    if date_position + 1 < len(texts):
        place = parse_location_string(texts[date_position + 1])
        location, work_mode = place["location"], place["work_mode"]

    dates = parse_date_range_string(texts[date_position])
    return [
        Experience(
            title=title,
            company=Company(
                name=company_name, public_id=public_id, url=entity_url
            ),
            employment_type=parsed["employment_type"],
            location=location,
            work_mode=work_mode,
            start=_as_partial(dates["start"]),
            end=_as_partial(dates["end"]),
            is_current=dates["is_current"],
            duration_months=dates["duration_months"],
        )
    ]


def _map_education(
    nodes: Sequence[TextNode], confidence: dict[str, Provenance]
) -> list[Education]:
    out: list[Education] = []
    for group in _group_by_entity(nodes):
        texts = [n.text for n in group]
        if not texts:
            continue
        school = texts[0]
        degree = field = None
        dates: dict = {"start": None, "end": None}

        for text in texts[1:]:
            if _is_date_range(text):
                dates = parse_date_range_string(text)
            elif degree is None:
                # "Bachelor of Engineering - BE, Electrical, Electronics..."
                # The comma splits qualification from field of study; the hyphen
                # belongs to the degree name and must not be split on.
                if "," in text:
                    degree, _, field = (part.strip() for part in text.partition(","))
                else:
                    degree = text
            elif field is None:
                field = text

        index = len(out)
        confidence[f"education[{index}].start"] = Provenance.PARSED
        confidence[f"education[{index}].end"] = Provenance.PARSED
        out.append(
            Education(
                school=school,
                school_public_id=_company_public_id(group[0].entity_url),
                school_url=group[0].entity_url,
                degree=degree,
                field=field,
                start=_as_partial(dates["start"]),
                end=_as_partial(dates["end"]),
            )
        )
    return out


def _map_skills(nodes: Sequence[TextNode]) -> list[Skill]:
    """Separate skills from the supporting detail rendered beneath them.

    The skills card interleaves each skill with evidence for it, and the two are
    not structurally nested — they are siblings in the same text run::

        Wireshark                                  bold    <- the skill
        Certified Network Security Practitioner    normal  <- evidence
        Cyber                                      bold    <- the skill
        Cybersecurity Fundamentals                 normal  <- evidence

    Treating every line as a skill turns a person's certifications into
    fabricated skills, which is exactly the kind of plausible-looking wrong
    output AGENTS.md §12 forbids. Cross-checking against LinkedIn's own PDF
    export confirmed the true list is the bold lines alone.

    Font weight is the only signal available. Where a card declares none, the
    parser falls back to treating each line as a skill, which is the previous
    behaviour and correct for cards that render endorsements instead.
    """
    out: list[Skill] = []
    weights = {n.emphasis for n in nodes if n.emphasis}
    styled = "bold" in weights

    for node in nodes:
        text = node.text
        if text in _HEADERS:
            continue

        match = _ENDORSEMENTS_RE.match(text)
        if match:
            if out:
                out[-1].endorsement_count = _int(match.group(1))
            continue

        if styled and node.emphasis != "bold":
            # Supporting evidence for the skill above it.
            if out:
                out[-1].credentials.append(text)
            continue

        out.append(Skill(name=text))

    return out


def _unwrap_safety_url(url: str | None) -> str | None:
    """Recover the real target from a LinkedIn /safety/go/ redirect.

    Credential links are wrapped:
    ``/safety/go/?url=https%3A%2F%2Fwww%2Ecoursera%2Eorg%2F...&urlhash=...``
    Note that dots are percent-encoded too, so a naive split is not enough.
    """
    if not url or "/safety/go/" not in url:
        return url
    query = urlparse(url).query
    target = parse_qs(query).get("url", [None])[0]
    return unquote(target) if target else url


def _map_certifications(nodes: Sequence[TextNode]) -> list[Certification]:
    """Assemble certifications from their rendered lines.

    An entry runs to four lines, and the last two are optional::

        Palo Alto Networks Cybersecurity Foundation   <- name
        Palo Alto Networks                            <- issuing authority
        Issued Jul 2026                               <- issue date
        Credential ID 6EDIYGC9PDCH                    <- credential id

    A "Show credential" control may follow, carrying the credential URL on its
    entity_url. Because the trailing lines are optional, entry boundaries are
    detected by the *arrival of a second plain line* after a name, rather than
    by counting — a certification with no authority would otherwise swallow the
    next entry's name.
    """
    out: list[Certification] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current and current.get("name"):
            out.append(Certification(**current))
        current = None

    skip_next = False
    for node in nodes:
        text = node.text

        if skip_next:
            skip_next = False
            continue
        # "Skills:" plus the list it introduces; and the build-version and
        # empty-state strings that trail a paged response. None are content, and
        # each would otherwise be read as a certification name or authority.
        if _SKILLS_LABEL_RE.match(text):
            skip_next = True
            continue
        if _VERSION_RE.match(text) or text.startswith(_EMPTY_STATE):
            continue
        if text.lower().startswith("licenses & certifications that"):
            continue

        issued = _ISSUED_RE.match(text)
        if issued:
            if current is not None:
                current["issued"] = _as_partial(
                    parse_date_range_string(issued.group(1))["start"]
                )
            continue

        credential = _CREDENTIAL_RE.match(text)
        if credential:
            if current is not None:
                current["credential_id"] = credential.group(1).strip()
            continue

        if text.lower().startswith("show credential"):
            if current is not None and node.entity_url:
                current["url"] = _unwrap_safety_url(node.entity_url)
            continue

        # A plain line: either this entry's authority, or the next entry's name.
        if current is None:
            current = {"name": text}
        elif "authority" not in current:
            current["authority"] = text
        else:
            flush()
            current = {"name": text}

    flush()
    return out


_ISSUED_BY_RE = re.compile(r"^Issued by\s+(.+)$", re.I)
_ASSOCIATED_RE = re.compile(r"^Associated with\s+(.+)$", re.I)
#: Recommendation tab headers: "Received (2)", "Given (2)".
_RECOMMEND_TAB_RE = re.compile(r"^(Received|Given)\s*\((\d+)\)$", re.I)
#: A bare tab label that opens a list of recommendation bodies.
#:
#: "Pending" must be listed even though it is not mapped: it has to be
#: recognised as a *boundary*, or its entries continue into whichever list was
#: open and are counted twice. In the captured data Pending repeats the Received
#: entries verbatim.
_RECOMMEND_GROUP_RE = re.compile(r"^(Received|Given|Pending)$", re.I)
#: Connection distance, rendered as a separate node: "· 3rd+".
_DEGREE_RE = re.compile(r"^[\u00b7\s]*(1st|2nd|3rd\+?|3rd)\s*$", re.I)
#: "February 26, 2025, the member was Alex's client"
_CONTEXT_DATE_RE = re.compile(r"^([A-Z][a-z]+\s+\d{1,2},\s*(\d{4}))\s*,")


def _group_by_component(nodes: Sequence[TextNode]) -> list[list[TextNode]]:
    """Split a run into entries using ``componentKey`` as the boundary.

    Every line of one entry shares a componentKey, which makes it an exact
    entry delimiter rather than an inference from text shape. Where a card
    declares no key the whole run is returned as a single group, and the caller
    falls back to content heuristics.
    """
    groups: list[list[TextNode]] = []
    for node in nodes:
        if groups and groups[-1][0].component_key == node.component_key:
            groups[-1].append(node)
        else:
            groups.append([node])
    return groups


def _map_honors(nodes: Sequence[TextNode]) -> list[Honor]:
    """Assemble honours and awards.

    An entry renders as a title plus up to three optional qualifier lines::

        Example Student Award         <- title
        Issued by EXAMPLE-PUBLISHER · Dec 2024           <- issuer + date
        Associated with EXAMPLE-COLLEGE <- association
        Received the prestigious Example Student Award...  <- description

    Two of the three qualifiers announce themselves with a prefix, but the
    description does not — and a description is indistinguishable by content
    from the *next* entry's title. Entries are therefore delimited by
    ``componentKey``, which every line of one entry shares.
    """
    out: list[Honor] = []

    for group in _group_by_component(nodes):
        entry: dict[str, Any] = {}
        for node in group:
            text = node.text
            if text in _HEADERS:
                continue

            issued_by = _ISSUED_BY_RE.match(text)
            if issued_by:
                issuer, _, when = issued_by.group(1).partition(MIDDLE_DOT)
                entry["issuer"] = issuer.strip() or None
                if when.strip():
                    entry["issued"] = _as_partial(
                        parse_date_range_string(when.strip())["start"]
                    )
                continue

            associated = _ASSOCIATED_RE.match(text)
            if associated:
                entry["associated_with"] = associated.group(1).strip()
                continue

            if "title" not in entry:
                entry["title"] = text
            elif "description" not in entry:
                entry["description"] = text

        if entry.get("title"):
            out.append(Honor(**entry))

    return out


def _map_recommendation_bodies(
    nodes: Sequence[TextNode],
) -> tuple[list[Recommendation], list[Recommendation]]:
    """Read recommendation entries, split into received and given.

    Each entry renders as a fixed run::

        Alex Doe                                     <- author
        \u00b7 3rd+                                        <- connection distance
        Chief Technologist    <- author headline
        February 26, 2025, the member was Alex's client      <- relationship + date
        Example recommendation text...  <- body, one node per para

    The author's name has no marker of its own; what identifies it is that the
    **next** line is a connection distance. Entries are therefore delimited by a
    one-node lookahead rather than by tracking state forwards, which is what
    made an earlier attempt absorb each author into the previous entry's body.

    Three tab groups are emitted — Received, Given and Pending. Pending repeats
    the Received entries verbatim in the captured data, so it is not mapped;
    doing so would double-count. Only an explicit "Received"/"Given" heading
    switches the destination list.
    """
    groups: dict[str, list[Recommendation]] = {"received": [], "given": []}
    current: list[Recommendation] | None = None
    entry: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal entry
        if entry and current is not None:
            current.append(_finish_recommendation(entry))
        entry = None

    texts = [n.text for n in nodes]
    for index, text in enumerate(texts):
        if text in _HEADERS or _RECOMMEND_TAB_RE.match(text):
            continue

        group = _RECOMMEND_GROUP_RE.match(text)
        if group:
            flush()
            current = groups.get(group.group(1).lower())
            continue

        if _DEGREE_RE.match(text):
            continue  # consumed with its author, below

        # An author is any line immediately followed by a connection distance.
        if index + 1 < len(texts) and _DEGREE_RE.match(texts[index + 1]):
            flush()
            entry = {
                "author": text,
                "author_degree": texts[index + 1].lstrip("\u00b7 ").strip(),
            }
            continue

        if entry is None:
            continue

        if "author_headline" not in entry:
            entry["author_headline"] = text
            continue

        context = _CONTEXT_DATE_RE.match(text)
        if context and "context" not in entry:
            entry["context"] = text
            entry["date"] = _as_partial(
                parse_date_range_string(context.group(1))["start"]
            )
            continue

        entry.setdefault("paragraphs", []).append(text)

    flush()
    return groups["received"], groups["given"]


def _finish_recommendation(entry: dict[str, Any]) -> Recommendation:
    paragraphs = entry.pop("paragraphs", [])
    entry["text"] = "\n\n".join(p for p in paragraphs if p) or None
    entry.setdefault("author", "")
    return Recommendation(**entry)


#: Pronouns, rendered as their own top-card node.
_PRONOUNS_RE = re.compile(
    r"^(he/him|she/her|they/them|he/they|she/they|ze/hir|[A-Za-z]+/[A-Za-z]+)$",
    re.I,
)


def _map_top_card(nodes: Sequence[TextNode], profile: dict) -> None:
    """Read the top card into name, headline, pronouns, location and counts.

    Order is fixed, but which lines are present is not: pronouns, the current
    company and the follower count are all optional. Positional reads therefore
    have to be anchored on something stable.

    The anchor used here is the standalone middle dot that separates the
    location from the connection count::

        Reid Hoffman
        Co-Founder, LinkedIn, Manas AI & Inflection AI...
        He/Him                       <- optional
        United States                <- location: the line before the dot
        \u00b7
        Contact info

    Anchoring on the dot rather than on punctuation *within* the value is what
    matters: an earlier version required a comma, which silently dropped every
    single-token location — "United States", "London", "Bengaluru" — while
    working perfectly on "Seattle, Washington, United States".

    The sticky header repeats the name and headline, so duplicates are collapsed
    first, in document order.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for node in nodes:
        text = node.text
        if _VERIFICATION_RE.match(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)

    # Locate the separator that follows the location, before stripping markers.
    anchor = None
    for index, text in enumerate(ordered):
        if text == MIDDLE_DOT or text in _HEADERS:
            anchor = index
            break

    content: list[str] = []
    for index, text in enumerate(ordered):
        if text == MIDDLE_DOT or text in _HEADERS:
            continue

        followers = _FOLLOWERS_RE.match(text)
        if followers:
            profile["follower_count"] = _int(followers.group(1))
            continue
        if re.match(r"^\d[\d,]*\+?$", text) or text.lower() == "connections":
            profile.setdefault("_conn", []).append(text)
            continue
        if "pronouns" not in profile and _PRONOUNS_RE.match(text) and index >= 2:
            profile["pronouns"] = text
            continue

        content.append((index, text))

    values = [text for _, text in content]
    if values:
        profile["name"] = values[0]
    if len(values) > 1:
        profile["headline"] = values[1]

    # The location is the last content line before the separator.
    if anchor is not None:
        before = [text for index, text in content if index < anchor]
        if len(before) > 2:
            profile["location"] = before[-1]

    if "location" not in profile:
        # No separator in this rendering; fall back to the first line that
        # reads as a place rather than a pair of entities.
        for text in values[2:]:
            if "," in text and MIDDLE_DOT not in text:
                profile["location"] = text
                break


def build_profile(
    public_id: str,
    nodes: Iterable[TextNode],
    *,
    endpoint_used: str,
    components_used: Sequence[str] = (),
    profile_id: str | None = None,
    identity: dict[str, Any] | None = None,
) -> ProfileResponse:
    """Assemble a :class:`ProfileResponse` from parsed RSC text nodes."""
    collector = _Collector()
    for node in nodes:
        collector.add(node)

    confidence: dict[str, Provenance] = {}
    warnings = list(collector.warnings)

    top: dict = {}
    _map_top_card(collector.get(*SECTION_TOP_CARD), top)

    # About renders as one node per paragraph, not as a single block. Taking
    # only the first would silently truncate a multi-paragraph summary to its
    # opening line ("Hello connections,").
    #
    # Members also pad it with zero-width filler to make it render blank; that
    # is empty, not content.
    paragraphs = [
        text
        for node in collector.get(SECTION_ABOUT)
        if (text := visible(node.text)) and text != "About"
    ]
    about = "\n\n".join(paragraphs) or None

    experience = _map_experience(
        collector.get(SECTION_EXPERIENCE), confidence, warnings
    )
    education = _map_education(collector.get(*SECTION_EDUCATION), confidence)
    skills = _map_skills(collector.get(SECTION_SKILLS))
    certifications = _map_certifications(
        collector.get(SECTION_CERTIFICATIONS, SECTION_CERT_CREDENTIAL)
    )
    languages = [Language(name=n.text) for n in collector.get(SECTION_LANGUAGES)]
    courses = [Course(name=n.text) for n in collector.get(SECTION_COURSES)]
    honors = _map_honors(collector.get(SECTION_HONORS))

    # Volunteer causes render as one line of interests, dot-separated.
    causes: list[str] = []
    for node in collector.get(SECTION_VOLUNTEER):
        if node.text in _HEADERS or node.text == "Causes":
            continue
        causes.extend(
            part.strip() for part in re.split(r"[\u2022\u00b7]", node.text) if part.strip()
        )

    # Only the tab headers are present; the recommendation text is fetched
    # separately when a tab is clicked, via an endpoint not yet observed.
    received = given = None
    for node in collector.get(*SECTION_RECOMMENDATIONS):
        tab = _RECOMMEND_TAB_RE.match(node.text)
        if tab:
            if tab.group(1).lower() == "received":
                received = _int(tab.group(2))
            else:
                given = _int(tab.group(2))
    got, gave = _map_recommendation_bodies(
        collector.get(*SECTION_RECOMMENDATIONS, "unknown")
    )
    recommendations = (
        Recommendations(
            received_count=received, given_count=given,
            received=got, given=gave,
        )
        if received is not None or given is not None or got or gave
        else None
    )

    connection = None
    if top.get("_conn"):
        connection = " ".join(top["_conn"])

    counts = {
        "experience": experience, "education": education, "skills": skills,
        "certifications": certifications, "languages": languages, "courses": courses,
    }
    coverage = dict(collector.coverage)
    for label, items in counts.items():
        section_key = {
            "experience": SECTION_EXPERIENCE, "education": SECTION_EDUCATION[0],
            "skills": SECTION_SKILLS, "certifications": SECTION_CERTIFICATIONS,
            "languages": SECTION_LANGUAGES, "courses": SECTION_COURSES,
            "honors": SECTION_HONORS,
        }[label]
        entry = coverage.setdefault(section_key, SectionCoverage())
        entry.returned = len(items)
        entry.fetched = bool(items) or section_key in coverage

        # The "see all" control is rendered by the card whether or not we went
        # on to page the full list. Once we hold as many entries as LinkedIn
        # says exist, the list is complete regardless of that control.
        if entry.total is not None and entry.returned >= entry.total:
            entry.truncated = False

    partial = [label for label, items in counts.items() if not items]
    for label, items in counts.items():
        section_key = {
            "experience": SECTION_EXPERIENCE, "education": SECTION_EDUCATION[0],
            "skills": SECTION_SKILLS, "certifications": SECTION_CERTIFICATIONS,
            "languages": SECTION_LANGUAGES, "courses": SECTION_COURSES,
            "honors": SECTION_HONORS,
        }[label]
        cover = coverage.get(section_key)
        if cover and cover.truncated:
            total = cover.total
            warnings.append(
                f"{label}: LinkedIn returned {cover.returned} of "
                f"{total if total is not None else 'an unstated number of'} entries"
            )

    # The screen response does not always include the top card — observed
    # returning it on one request and omitting it on the next for the same
    # profile, with HTTP 200 both times. Falling back to the public id without
    # saying so would produce a confident-looking response whose "name" is
    # really a URL slug, which is the 200-with-nulls failure §12 forbids.
    if "name" not in top:
        warnings.append(
            "top card absent from the screen response: name, headline, location "
            "and follower count are unavailable. LinkedIn returns this "
            "intermittently; retry with ?refresh=true"
        )
        partial.append("top_card")

    identity = identity or {}

    name_text = top.get("name", public_id)
    parts = name_text.split()
    # Voyager supplies an authoritative first/last split, locale variants and
    # image expiry — none of which exist anywhere in the SDUI payload. Where it
    # answered, its typed values take precedence over anything recovered from
    # rendered text; where it did not, the SDUI-derived values stand.
    identity_name = identity.get("name")
    if identity_name is not None:
        confidence["name.first"] = Provenance.RAW
        confidence["name.last"] = Provenance.RAW
    else:
        confidence["name.first"] = Provenance.PARSED
        confidence["name.last"] = Provenance.PARSED

    profile = Profile(
        public_id=public_id,
        profile_url=f"https://www.linkedin.com/in/{public_id}/",
        profile_id=profile_id,
        profile_urn=f"urn:li:fsd_profile:{profile_id}" if profile_id else None,
        name=identity_name or Name(
            full=name_text,
            first=parts[0] if parts else None,
            last=parts[-1] if len(parts) > 1 else None,
            split_inferred=True,
        ),
        headline=top.get("headline") or identity.get("headline"),
        headline_locales=identity.get("headline_locales", {}),
        pronouns=top.get("pronouns"),
        about=about or identity.get("about"),
        about_locales=identity.get("about_locales", {}),
        primary_locale=identity.get("primary_locale"),
        websites=identity.get("websites", []),
        is_premium=identity.get("is_premium"),
        is_verified=identity.get("is_verified"),
        images=identity.get("images", {}),
        location=(
            Location(
                raw=top["location"], country_code=identity.get("country_code")
            )
            if top.get("location")
            else None
        ),
        follower_count=top.get("follower_count"),
        connection_count=connection,
        experience=experience,
        education=education,
        skills=skills,
        certifications=certifications,
        languages=languages,
        courses=courses,
        honors=honors,
        volunteer_causes=causes,
        recommendations=recommendations,
    )

    return ProfileResponse(
        profile=profile,
        meta=Meta(
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            endpoint_used=endpoint_used,
            components_used=list(components_used),
            coverage=coverage,
            parse_confidence=confidence,
            partial_fields=partial,
            warnings=warnings,
            completeness={
                label: (1.0 if items else 0.0) for label, items in counts.items()
            },
        ),
    )
