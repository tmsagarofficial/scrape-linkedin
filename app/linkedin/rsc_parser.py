"""Parser for LinkedIn's React Server Components (RSC) "flight" wire format.

LinkedIn retired the legacy Voyager `profileView` REST endpoint (it now returns
410 Gone) and serves profile content through a Server-Driven UI layer instead.
Those responses are not JSON documents; they are RSC flight streams, which are
newline-delimited records of the form::

    <hex_id>:<payload>

Three record kinds appear in a profile response:

``1:I["030d6035...",[],"default"]``
    A *module import* — registers a client component under an id. Carries no
    profile data, but tells us which component rendered a given subtree.

``2:null``
    A plain JSON value, referenced by id from elsewhere in the stream.

``0:["$","div",null,{...}]``
    A React element, encoded as ``["$", tag, key, props]``. ``tag`` is either an
    HTML tag name (``"div"``) or a reference to an imported component
    (``"$L4"``).

Records are emitted in *completion* order, not document order, and a record may
reference an id that appears later in the stream. Resolution is therefore a
two-pass operation: index every record first, then walk from the root.

References appear in two distinct forms, and both must be handled:

1. In an element's tag slot — ``["$", "$L22", null, {...}]``
2. As a bare string in a value slot — ``{"children": ["$L35", "$L36"]}``

Per RSC's escaping rule, a literal string that begins with ``$`` is encoded with
a doubled prefix (``"$$5.00"`` means the text ``"$5.00"``).

Ordering
--------
AGENTS.md §2.5 warns that filtering a flat collection by type yields elements in
the wrong order. The same hazard applies here: the records are *not* in document
order, so reading text by ascending line id produces plausible but wrong output.
This module resolves from the root element downward, which is the only way to
recover true rendered order.

Nothing in this module raises on malformed input. Anything unparseable is
recorded in :attr:`ParsedFlight.errors` and skipped, so that a partial profile
still yields the sections that did parse.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger(__name__)

#: A record line: hex id, colon, payload.
_LINE_RE = re.compile(r"^([0-9a-fA-F]+):(.*)$", re.DOTALL)

#: A lazy reference to another record, e.g. "$L35". Ids are hexadecimal.
_LAZY_REF_RE = re.compile(r"^\$L([0-9a-fA-F]+)$")

#: A *direct* reference to another record, e.g. "$35".
#:
#: RSC uses both forms and they are not interchangeable in the wire format,
#: though for our purposes both mean "substitute record N here". The small
#: component responses use almost only "$L"; a full profile screen is dominated
#: by the direct form, so handling only "$L" parses the stream without error and
#: yields nothing at all.
#:
#: Lowercase-hex-only is what disambiguates a reference from a sentinel:
#: "$undefined" and "$Sreact.fragment" both start with "$" but cannot match.
_DIRECT_REF_RE = re.compile(r"^\$([0-9a-f]+)$")

#: Props that hold styling or telemetry and must never be descended into.
#:
#: This is a *denylist*, deliberately. An allowlist of content props was tried
#: first and does not survive contact with a full screen response: alongside
#: ``children`` and ``initialContent``, subtrees also hang off ``navItemProps``,
#: ``renderedChildScreen``, ``renderedContentBefore``, ``renderedToolbar``,
#: ``resultsContainer`` and others, and the set differs per response. Enumerating
#: them is a losing game against LinkedIn's deploys — a missed name yields a
#: silent partial parse, which is the failure mode this module exists to avoid.
#:
#: Skipping these also keeps us out of large telemetry blobs.
_METADATA_PROPS = frozenset(
    {
        "className",
        "style",
        "viewTrackingSpecs",
        "trackingScope",
        "visibilityTriggers",
        "renderPayload",
        "triggers",
        "modelStates",
        "data-sdui-component",
        "data-sdui-screen",
    }
)

#: Keys that carry a subtree on a plain (non-element) dict.
_LEGACY_CONTENT_PROPS = ("children", "initialContent", "buttonProps", "textProps")

#: Keys that carry a subtree on a plain (non-element) dict.
#:
#: A full screen response opens with a list of route descriptors rather than an
#: element, and the UI tree hangs off ``component``::
#:
#:     [{"component": "$L1", "layoutId": "...profile.Profile#c4527215", ...}]
#:
#: Component-scoped responses have no such wrapper, which is why this is only
#: needed once the whole screen is fetched.
_SCREEN_PROPS = ("component",)

#: Characters that render as nothing but are not Unicode whitespace, so
#: ``str.strip()`` leaves them behind. LinkedIn's About field is padded with
#: HANGUL FILLER by members who want a visually blank summary.
_INVISIBLE = "\u3164\u200b\u200c\u200d\ufeff\u00a0"

#: Guard against pathological or cyclic documents.
_MAX_DEPTH = 200


@dataclass
class ParsedFlight:
    """The indexed contents of one RSC flight response."""

    #: Record id -> decoded JSON value, for data records.
    data: dict[str, Any] = field(default_factory=dict)
    #: Record id -> component name, for ``I[...]`` module-import records.
    imports: dict[str, str] = field(default_factory=dict)
    #: Human-readable notes about records that could not be decoded.
    errors: list[str] = field(default_factory=list)

    @property
    def root(self) -> Any:
        """The primary document root, conventionally record ``0``."""
        return self.data.get("0")

    def roots(self) -> list[str]:
        """Every record not referenced by another, in stream order.

        A response is not always a single tree. The recommendations card, for
        example, emits the tab headers under record ``0`` and the recommendation
        bodies under a **separate, unreferenced record** — nothing links the two.
        Walking only record ``0`` returns the tabs and silently discards the
        content, with no error to indicate anything was missed.

        Record ``0`` is returned first when present, since it is conventionally
        the primary tree; the rest follow in the order they arrived.
        """
        referenced: set[str] = set()

        def scan(value: Any, depth: int = 0) -> None:
            if depth > _MAX_DEPTH:
                return
            if isinstance(value, str):
                target = _ref_target(value)
                if target:
                    referenced.add(target)
            elif isinstance(value, list):
                for item in value:
                    scan(item, depth + 1)
            elif isinstance(value, dict):
                for item in value.values():
                    scan(item, depth + 1)

        for record in self.data.values():
            scan(record)

        unreferenced = [rid for rid in self.data if rid not in referenced]
        if "0" in unreferenced:
            unreferenced.remove("0")
            unreferenced.insert(0, "0")
        return unreferenced


@dataclass(frozen=True)
class TextNode:
    """One rendered string, with the context it was found in."""

    text: str
    #: Nearest enclosing ``viewTrackingSpecs.viewName``, e.g.
    #: ``"profile-card-education"``. This is LinkedIn's own section label.
    section: str | None = None
    #: Nearest enclosing navigation target, e.g. a company or school URL.
    #: Entries about the same entity share one, which makes it a grouping key.
    entity_url: str | None = None
    #: Nearest enclosing ``componentKey`` (a per-render UUID).
    component_key: str | None = None
    #: ``textProps.fontWeight``, where the component declared one.
    #:
    #: Styling is normally noise, but on some cards it is the *only* signal
    #: separating an entry from its supporting detail. The skills card renders
    #: the skill name bold and the credentials evidencing it at normal weight,
    #: with no structural nesting to tell them apart.
    emphasis: str | None = None


@dataclass
class _Ctx:
    """Context inherited down the tree as we walk it."""

    section: str | None = None
    entity_url: str | None = None
    component_key: str | None = None
    emphasis: str | None = None

    def merged(self, props: dict[str, Any]) -> "_Ctx":
        """Return a context updated with any signals present in ``props``."""
        return _Ctx(
            section=_view_name(props) or _section_hint(props) or self.section,
            entity_url=_navigate_url(props) or self.entity_url,
            component_key=props.get("componentKey")
            or props.get("componentkey")
            or self.component_key,
            emphasis=_font_weight(props) or self.emphasis,
        )


def parse_flight(body: str) -> ParsedFlight:
    """Index every record in an RSC flight response.

    Malformed records are collected into :attr:`ParsedFlight.errors` rather than
    raising, so one bad line cannot cost us the whole profile.
    """
    flight = ParsedFlight()

    for lineno, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            continue

        match = _LINE_RE.match(line)
        if not match:
            flight.errors.append(f"line {lineno}: no <id>:<payload> prefix")
            continue

        record_id, payload = match.group(1), match.group(2)

        # Module imports: I["<hash>",[<deps>],"<ComponentName>"]
        if payload.startswith("I["):
            try:
                _, _, name = json.loads(payload[1:])
                flight.imports[record_id] = name
            except (ValueError, TypeError):
                flight.errors.append(f"line {lineno}: bad import record")
            continue

        try:
            flight.data[record_id] = json.loads(payload)
        except ValueError as exc:
            flight.errors.append(f"line {lineno} (id {record_id}): {exc}")

    if not flight.data:
        log.warning("RSC response contained no decodable data records")

    return flight


def iter_text(flight: ParsedFlight) -> Iterator[TextNode]:
    """Yield every rendered string, in true document order.

    Walks from the root element, resolving references as it goes, and descends
    only into content-bearing props so that class names, style tokens and
    telemetry payloads are never mistaken for profile text.
    """
    roots = flight.roots()
    if not roots:
        log.warning("RSC response has no root record; nothing to extract")
        return
    for record_id in roots:
        yield from _walk(
            flight.data[record_id], flight, _Ctx(), depth=0, seen=frozenset()
        )


def _walk(
    node: Any,
    flight: ParsedFlight,
    ctx: _Ctx,
    depth: int,
    seen: frozenset[str],
    in_list: bool = False,
) -> Iterator[TextNode]:
    """Recursively emit text nodes from ``node``.

    ``seen`` tracks the record ids on the current path so a self-referential
    document terminates instead of recursing forever.

    ``in_list`` carries the one structural fact that separates content from
    metadata: **rendered text is always an element of a list**
    (``{"children": ["Amazon"]}``), while metadata is always a scalar dict value
    (``{"className": "_02484ad3"}``). Emitting only list members keeps class
    names, tracking ids and accessibility labels out of the output without
    needing to know what every prop is called.
    """
    if depth > _MAX_DEPTH:
        log.debug("max depth reached; truncating branch")
        return

    if isinstance(node, str):
        yield from _walk_str(node, flight, ctx, depth, seen, in_list)
        return

    if isinstance(node, list):
        if _is_element(node):
            yield from _walk_element(node, flight, ctx, depth, seen)
        else:
            for child in node:
                yield from _walk(child, flight, ctx, depth + 1, seen, in_list=True)
        return

    if isinstance(node, dict):
        for key, value in node.items():
            if key in _METADATA_PROPS:
                continue
            yield from _walk(value, flight, ctx, depth + 1, seen, in_list=False)


def _walk_str(
    node: str,
    flight: ParsedFlight,
    ctx: _Ctx,
    depth: int,
    seen: frozenset[str],
    in_list: bool = False,
) -> Iterator[TextNode]:
    """Handle a string slot: a reference, an escaped literal, or plain text."""
    target = _ref_target(node)
    if target:
        if target in seen:
            log.debug("cycle detected at record %s; not re-entering", target)
            return
        if target not in flight.data:
            # Referenced record is an import or simply absent.
            return
        yield from _walk(
            flight.data[target], flight, ctx, depth + 1, seen | {target}, in_list
        )
        return

    # RSC escapes a literal leading "$" by doubling it.
    if node.startswith("$$"):
        node = node[1:]
    elif node.startswith("$"):
        return  # sentinel such as "$" or "$undefined"

    text = node.strip()
    if _is_serialised_json(text):
        # Some cards carry a JSON blob in a value slot rather than a prop. It
        # satisfies the "string inside a list" rule but is plainly not rendered
        # text, and would otherwise surface as a profile field.
        log.debug("skipping serialised JSON in a text slot")
        return
    if text and in_list:
        yield TextNode(
            text=text,
            section=ctx.section,
            entity_url=ctx.entity_url,
            component_key=ctx.component_key,
            emphasis=ctx.emphasis,
        )


def _walk_element(
    node: list[Any],
    flight: ParsedFlight,
    ctx: _Ctx,
    depth: int,
    seen: frozenset[str],
) -> Iterator[TextNode]:
    """Handle ``["$", tag, key, props]``, updating context from its props."""
    tag, props = node[1], node[3]

    if not isinstance(props, dict):
        return

    ctx = ctx.merged(props)

    # A component tag may itself be a reference; its props still hold the
    # content, so we only need to follow the content props below.
    if isinstance(tag, str) and _ref_target(tag):
        pass

    for key, value in props.items():
        if key in _METADATA_PROPS:
            continue
        yield from _walk(value, flight, ctx, depth + 1, seen, in_list=False)


def _ref_target(value: str) -> str | None:
    """Return the record id ``value`` refers to, or None if it is not a ref."""
    match = _LAZY_REF_RE.match(value) or _DIRECT_REF_RE.match(value)
    return match.group(1) if match else None


def _is_serialised_json(text: str) -> bool:
    """True if ``text`` is a JSON object or array rather than display text."""
    if len(text) < 2 or text[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def visible(text: str | None) -> str | None:
    """Strip whitespace *and* zero-width characters; None if nothing remains.

    A field padded with invisible filler is empty in every sense that matters to
    a consumer, and must not be reported as content.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip().strip(_INVISIBLE).strip()
    return cleaned or None


def _is_element(node: list[Any]) -> bool:
    """True if ``node`` is a React element tuple ``["$", tag, key, props]``."""
    return len(node) >= 4 and node[0] == "$"


def _view_name(props: dict[str, Any]) -> str | None:
    """Extract LinkedIn's own section label from a props dict.

    ``viewTrackingSpecs`` is telemetry, but it carries semantic section names
    (``profile-card-experience``, ``profile-card-education``) that are far more
    reliable than inferring boundaries from text position.
    """
    specs = props.get("viewTrackingSpecs")
    if isinstance(specs, dict):
        name = specs.get("viewName")
        return name if isinstance(name, str) else None
    if isinstance(specs, list):
        for spec in specs:
            if isinstance(spec, dict) and isinstance(spec.get("viewName"), str):
                return spec["viewName"]
    return None


def _font_weight(props: dict[str, Any]) -> str | None:
    """Read ``textProps.fontWeight``, if this element declares one."""
    text_props = props.get("textProps")
    if isinstance(text_props, dict):
        weight = text_props.get("fontWeight")
        if isinstance(weight, str):
            return weight
    return None


def _section_hint(props: dict[str, Any]) -> str | None:
    """Derive a section label from a component's observability identifier.

    The top-level cards are wrapped in components identified as
    ``com.linkedin.sdui.impl.profile.components.experienceTopLevelSection``.
    This fires one level above ``viewTrackingSpecs`` and so labels the card
    header as well as its entries.
    """
    ident = props.get("observabilityIdentifier")
    if isinstance(ident, str) and ident:
        return ident.rsplit(".", 1)[-1]
    return None


def _navigate_url(props: dict[str, Any]) -> str | None:
    """Find the navigation target of a click trigger, if this element has one.

    Entries for the same company or school carry the same URL, which gives us a
    stable entity key that survives changes to LinkedIn's visual layout.
    """
    found: list[str] = []

    def scan(value: Any, depth: int = 0) -> None:
        if found or depth > 12:
            return
        if isinstance(value, dict):
            type_name = value.get("$type")
            if isinstance(type_name, str) and type_name.endswith("NavigateToUrl"):
                url = value.get("urlValue")
                if isinstance(url, dict) and isinstance(url.get("url"), str):
                    found.append(url["url"])
                    return
            for key, sub in value.items():
                if key in _LEGACY_CONTENT_PROPS:
                    continue  # content is walked separately, not scanned
                scan(sub, depth + 1)
        elif isinstance(value, list):
            for sub in value:
                scan(sub, depth + 1)

    for key in ("triggers", "action", "actions", "onClick", "navigation"):
        if key in props:
            scan(props[key])

    return found[0] if found else None
