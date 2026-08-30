"""Live client for LinkedIn's SDUI profile endpoints.

The request shapes here are not guesses; each was verified against LinkedIn with
a real session, and every call is recorded in `docs/evidence/request-log.jsonl`.
What the probes established:

* ``GET /flagship-web/in/{public_id}`` with ``x-li-rsc-stream: true`` returns an
  RSC flight stream to a plain HTTP client — no browser, and no 999.
* ``miniProfileUrn`` is **not** required. The vanity slug alone is enough, so a
  profile URL resolves in one request rather than a lookup plus a fetch.
* ``x-li-pageforestid``, ``x-li-page-instance`` and the traceparent headers are
  **not validated server-side**. They were sent freshly generated, not replayed,
  and the request succeeded. No warm-up request is needed.
* The 3 KB component POST body is reconstructible from the vanity name, and
  tolerates omitting most of ``profileComponentState``.

Section-to-component mapping, resolved by probing each component once:

========================================  =================================
Component                                 Sections
========================================  =================================
profileCardsAboveActivity                 about, featured
profileCardsBelowActivityPart1            experience, education, licenses
profileCardsBelowActivityPart2            recommendations
profileCardsBelowActivityPart3            courses, honors, patents, papers
profileCardsBelowActivityPart4            languages, organizations
profileCardsBelowActivityPart5            interests
profileCardsBelowActivityPart6            volunteer causes
profileCardsBelowActivityPart7            skills
========================================  =================================

Not every component is a numbered part, so components are addressed by name.

Part 5 (interests) is excluded by default: at ~196 KB it is roughly 70% of the
total payload and maps to no field in the public schema.

Detail screens also exist — ``skills``, ``certifications`` and
``profileCardsExperienceOnly`` are what LinkedIn's own "Show all" controls
navigate to, and return the untruncated list. They are not wired up yet; see
METHODOLOGY.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Sequence

from app.config import Settings
from app.linkedin import request_log
from app.linkedin.rsc_parser import TextNode, iter_text, parse_flight
from app.linkedin.voyager import DASH_PROFILES, parse_identity

log = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
DSL = "com.linkedin.sdui.generated.profile.dsl.impl"
SCREEN_ID = "com.linkedin.sdui.flagshipnav.profile.Profile"
CLIENT_VERSION = "0.2.7003"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

#: Public schema section -> the component that renders it. Not all components
#: are numbered parts: About lives in a separately named one.
SECTION_COMPONENTS: dict[str, str] = {
    "about": "profileCardsAboveActivity",
    "experience": "profileCardsBelowActivityPart1",
    "education": "profileCardsBelowActivityPart1",
    "certifications": "profileCardsBelowActivityPart1",
    "courses": "profileCardsBelowActivityPart3",
    "honors": "profileCardsBelowActivityPart3",
    "recommendations": "profileCardsBelowActivityPart2",
    "volunteer_causes": "profileCardsBelowActivityPart6",
    "languages": "profileCardsBelowActivityPart4",
    "skills": "profileCardsBelowActivityPart7",
}

#: Fetched when the caller does not narrow with `fields`.
DEFAULT_SECTIONS = (
    "about", "experience", "education", "certifications", "languages", "skills",
    "honors",
)

_STATE_KEYS = (
    ("shouldRefreshScreenOnReappear", "ShouldRefreshScreen"),
    ("shouldFetchFromCache", "FetchFromCache"),
    ("shouldDisplayTabAnchors", "ShouldDisplayTabAnchors"),
    ("shouldReloadTopCardOnReappear", "ShouldReloadTopCardOnReappear"),
)

_PROFILE_ID_RE = re.compile(r"ACoAA[A-Za-z0-9_-]{20,}")

#: Detail screens, reached by LinkedIn's own "Show all" controls.
#:
#: The cards cap how many entries they render; these screens do not. Each is a
#: POST to /in/{vanity}/details/{slug}/ carrying a NavigateToScreen action whose
#: only variable inputs are the vanity name and the screen id.
#:
#: section -> (url slug, screen id suffix, pageKey fragment)
DETAIL_SCREENS: dict[str, tuple[str, str, str]] = {
    "skills": ("skills", "ProfileSkillDetails", "skills"),
    "certifications": ("certifications", "ProfileCertificationDetails", "certifications"),
    "experience": ("experience", "ProfileExperienceDetails", "experience"),
    "education": ("education", "ProfileEducationDetails", "education"),
    "languages": ("languages", "ProfileLanguageDetails", "languages"),
    "honors": ("honors", "ProfileHonorDetails", "honors"),
    "courses": ("courses", "ProfileCourseDetails", "courses"),
}

#: Paged detail feeds. The detail *screen* returns only chrome and filter tabs;
#: the entries come from a pager, which is where truncation is actually solved.
#:
#: Paging uses ``start`` and ``count`` — the same parameter names the legacy
#: Voyager API used (AGENTS.md §2.7), which survived the SDUI migration intact.
#:
#: section -> (pager id suffix, screen id suffix, extra payload)
DETAIL_PAGERS: dict[str, tuple[str, str, dict[str, str]]] = {
    "skills": ("skills", "ProfileSkillDetails", {"filter": "ProfileSkillCategory_ALL"}),
    "certifications": ("certifications", "ProfileCertificationDetails", {}),
    "experience": ("experience", "ProfileExperienceDetails", {}),
    "education": ("education", "ProfileEducationDetails", {}),
    "languages": ("languages", "ProfileLanguageDetails", {}),
    "honors": ("honors", "ProfileHonorDetails", {}),
    "courses": ("courses", "ProfileCourseDetails", {}),
}

#: Pager responses carry no viewTrackingSpecs, so their nodes arrive unlabelled.
#: They are re-tagged with the label the card would have used, so the mapper
#: does not need to know which fetch path produced them.
PAGER_SECTION_LABELS: dict[str, str] = {
    "skills": "profile-card-skills",
    "certifications": "profile-card-licenses-and-certifications",
    "experience": "profile-card-experience",
    "education": "education-lockup-view",
    "languages": "profile-card-languages",
    "honors": "profile-card-honors-and-awards",
    "courses": "profile-card-courses",
}

#: Redirect ceiling. The default is 30, which turns a self-redirect into a
#: 30-hop round trip repeated on every retry — slow, and pointless once the
#: loop is established.
MAX_REDIRECTS = 5

#: Transport-level retries. AGENTS.md §7 asks for exponential backoff with
#: jitter; a connection reset mid-survey is transient and retrying it is much
#: cheaper than failing a profile and re-running the whole sample.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.5

#: Entries per pager request, matching what the web client asks for.
PAGE_SIZE = 10

#: Stop paging after this many requests, whatever the upstream says. A profile
#: with 200 skills is not worth 20 authenticated requests, and an endpoint that
#: never signals exhaustion must not loop forever.
MAX_PAGES = 5


class LinkedInError(Exception):
    """Base class for upstream failures, carrying a taxonomy hint."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class ProfileNotFound(LinkedInError):
    def __init__(self, public_id: str) -> None:
        super().__init__(f"no such profile: {public_id}", status=404)


class ProfileNotVisible(LinkedInError):
    def __init__(self, public_id: str) -> None:
        super().__init__(
            f"profile {public_id} is not visible to the authenticated session",
            status=403,
        )


class UpstreamBlocked(LinkedInError):
    """HTTP 999, or a session LinkedIn no longer accepts."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status=503)


class SessionExpired(LinkedInError):
    def __init__(self) -> None:
        super().__init__("LinkedIn session is expired or absent", status=503)


class RedirectLoop(LinkedInError):
    """The endpoint redirected to itself.

    Observed on a profile that had answered normally minutes earlier: the
    screen URL began returning 302 to its own address. A self-redirect is not a
    network blip — it is LinkedIn declining to serve this session this resource,
    usually behind an interstitial — so retrying it is pointless and it is worth
    reporting as its own condition rather than as a generic transport failure.
    """

    def __init__(self, url: str) -> None:
        super().__init__(
            f"LinkedIn redirected {url} to itself; the session is likely being "
            "shown an interstitial for this profile",
            status=503,
        )


@dataclass
class FetchResult:
    nodes: list[TextNode] = field(default_factory=list)
    components_used: list[str] = field(default_factory=list)
    profile_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Typed identity fields from the Voyager Dash record, where available.
    identity: dict = field(default_factory=dict)


#: Sections that only the top card emits.
_TOP_CARD_SECTIONS = ("profile-top-card", "profile-sticky-header")


def _has_top_card(nodes: Sequence[TextNode]) -> bool:
    return any((n.section or "") in _TOP_CARD_SECTIONS for n in nodes)


def _telemetry() -> dict[str, str]:
    """Fresh, well-formed telemetry ids.

    Proven acceptable server-side: the probe generated these rather than
    replaying captured values and still received 200.
    """
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    tracking = base64.b64encode(secrets.token_bytes(16)).decode()
    return {
        "x-li-pageforestid": trace,
        "x-li-traceparent": f"00-{trace}-{span}-00",
        "x-li-tracestate": f"LinkedIn={span}",
        "x-li-page-instance": f"urn:li:page:p_flagship3_profile_view_base;{tracking}",
        "x-li-page-instance-tracking-id": tracking,
    }


class LinkedInClient:
    """Fetches and parses profile sections over the SDUI endpoints."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- plumbing ---------------------------------------------------------

    def _session(self):
        try:
            from curl_cffi import requests as cffi
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise UpstreamBlocked("curl_cffi is not installed") from exc
        return cffi

    def _cookies(self) -> dict[str, str]:
        return {
            "li_at": self.settings.li_at,
            "JSESSIONID": self.settings.jsessionid,
        }

    def _headers(
        self, public_id: str, *, post: bool = False, referer: str | None = None
    ) -> dict[str, str]:
        headers = {
            # §2.2 — mandatory outside a browser, else 400 invalid hostname.
            "Host": "www.linkedin.com",
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "csrf-token": self.settings.csrf_token,
            "referer": referer or f"{BASE}/in/{public_id}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "x-li-application-version": CLIENT_VERSION,
            "x-li-rsc-stream": "true",
            "x-li-track": json.dumps(
                {
                    "clientVersion": CLIENT_VERSION,
                    "mpVersion": CLIENT_VERSION,
                    "osName": "web",
                    "timezoneOffset": 5.5,
                    "timezone": "Asia/Calcutta",
                    "deviceFormFactor": "DESKTOP",
                    "mpName": "flagship-web",
                },
                separators=(",", ":"),
            ),
        }
        if post:
            headers["content-type"] = "application/json"
            headers["origin"] = BASE
        headers.update(_telemetry())
        return headers

    def _request(
        self,
        method: str,
        url: str,
        public_id: str,
        body: str | None = None,
        referer: str | None = None,
        headers_override: dict[str, str] | None = None,
    ):
        if not self.settings.has_session:
            raise SessionExpired()

        cffi = self._session()
        headers = headers_override or self._headers(
            public_id, post=body is not None, referer=referer
        )
        kwargs = {
            "headers": headers,
            "cookies": self._cookies(),
            "impersonate": self.settings.impersonate,
            "timeout": 30,
            "max_redirects": MAX_REDIRECTS,
        }
        if self.settings.proxy_url:
            # §11: LinkedIn traffic only. Our own responses serve direct.
            kwargs["proxies"] = {
                "http": self.settings.proxy_url,
                "https": self.settings.proxy_url,
            }
        if body is not None:
            kwargs["data"] = body

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = getattr(cffi, method.lower())(url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced as a clean 503
                # A self-redirect will not resolve by trying again.
                if type(exc).__name__ == "TooManyRedirects":
                    request_log.record(
                        method=method, url=url, headers=headers,
                        cookies=self._cookies(), status=0, response_headers={},
                        body=b"", request_body=body,
                        impersonate=self.settings.impersonate,
                        note="redirect loop; not retried",
                    )
                    raise RedirectLoop(url) from exc
                last_error = exc
                # A transport failure never reaches the server, so it would
                # otherwise leave no trace at all. The audit log is only
                # trustworthy if it records attempts, not just successes.
                request_log.record(
                    method=method, url=url, headers=headers,
                    cookies=self._cookies(), status=0, response_headers={},
                    body=b"", request_body=body,
                    impersonate=self.settings.impersonate,
                    note=f"transport failure (attempt {attempt}): {exc}",
                )
                if attempt < MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise UpstreamBlocked(
                    f"request to LinkedIn failed after {MAX_ATTEMPTS} attempts: "
                    f"{exc}"
                ) from exc

            content = response.content or b""
            request_log.record(
                method=method, url=url, headers=headers, cookies=self._cookies(),
                status=response.status_code,
                response_headers=dict(response.headers), body=content,
                request_body=body, impersonate=self.settings.impersonate,
            )

            # 429 and 999 are worth one more try after a pause; everything else
            # is a definite answer and is translated immediately.
            if response.status_code in (429, 999) and attempt < MAX_ATTEMPTS:
                log.warning(
                    "upstream returned %s; backing off", response.status_code
                )
                self._backoff(attempt)
                continue

            self._raise_for_status(response.status_code, content, public_id)
            return content.decode("utf-8", "replace")

        raise UpstreamBlocked(f"request to LinkedIn failed: {last_error}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff with jitter, per AGENTS.md §7.

        Jitter matters when several requests fail together: without it they all
        retry at the same instant and reproduce the burst that caused the
        failure.
        """
        delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        time.sleep(delay + random.uniform(0, delay * 0.3))

    @staticmethod
    def _raise_for_status(status: int, content: bytes, public_id: str) -> None:
        """Translate an upstream status into the §5 error taxonomy."""
        if status == 200:
            head = content[:200].decode("utf-8", "ignore").lstrip()
            if head.lower().startswith(("<!doctype", "<html")):
                # A hard-navigation HTML response means the RSC path was not
                # taken; usually the session is no longer accepted.
                raise SessionExpired()
            return
        if status == 999:
            raise UpstreamBlocked(
                "LinkedIn returned 999 (automation defence). See AGENTS.md §2.1"
            )
        if status == 404:
            raise ProfileNotFound(public_id)
        if status in (401, 403):
            raise ProfileNotVisible(public_id)
        if status == 410:
            raise UpstreamBlocked("endpoint retired by LinkedIn (410 Gone)")
        if status == 429:
            raise LinkedInError("rate limited by LinkedIn", status=429)
        raise UpstreamBlocked(f"unexpected upstream status {status}")

    # -- requests ---------------------------------------------------------

    def fetch_screen(self, public_id: str) -> tuple[str, str | None]:
        """Fetch the profile screen shell: top card, plus the durable id."""
        body = self._request("GET", f"{BASE}/flagship-web/in/{public_id}", public_id)
        match = _PROFILE_ID_RE.search(body)
        return body, match.group(0)[:39] if match else None

    def fetch_component(self, public_id: str, profile_id: str, name: str) -> str:
        """Fetch one named SDUI component for this profile."""
        component = f"{DSL}.{name}"
        url = (
            f"{BASE}/flagship-web/rsc-action/actions/component"
            f"?componentId={component}&sduiid={component}"
        )
        return self._request(
            "POST", url, public_id, body=self._component_body(public_id, profile_id)
        )

    @staticmethod
    def _component_body(public_id: str, profile_id: str) -> str:
        state: dict = {"profileId": public_id}
        for field_name, key_name in _STATE_KEYS:
            state[field_name] = {
                "type": "com.linkedin.sdui.components.core.BindingImpl",
                "value": {
                    "key": f"ProfileComponentState{key_name}{public_id}"
                           "ProfileComponentState",
                    "namespace": "MemoryNamespace",
                },
            }
        return json.dumps(
            {
                "clientArguments": {
                    "payload": {
                        "isSelfView": False,
                        "vanityName": public_id,
                        "replaceableSectionArgs": {
                            "vanityName": public_id,
                            "hideCardsForGoldenGate": False,
                            "shouldSetupReplaceableComponent": True,
                            "vieweeProfileId": profile_id,
                            "isSelfView": False,
                            "isSelfViewResolved": False,
                        },
                        "profileComponentState": state,
                    },
                    "states": [],
                    "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
                    "screenId": SCREEN_ID,
                    "knownTemplateIds": [],
                }
            },
            separators=(",", ":"),
        )

    def fetch_detail(self, public_id: str, section: str) -> str:
        """Fetch a section's full, untruncated detail screen.

        Raises KeyError for a section with no known detail screen.
        """
        slug, screen, page = DETAIL_SCREENS[section]
        screen_id = f"com.linkedin.sdui.flagshipnav.profile.{screen}"
        url = f"{BASE}/flagship-web/in/{public_id}/details/{slug}/"
        body = json.dumps(
            {
                "$type": "proto.sdui.actions.core.NavigateToScreen",
                "screenId": screen_id,
                "pageKey": f"profile_view_base_{page}_details",
                "presentationStyle": "PresentationStyle_FULL_PAGE",
                "presentation": {
                    "$case": "fullPage",
                    "fullPage": {
                        "$type": "proto.sdui.actions.core.presentation."
                                 "FullPagePresentation"
                    },
                },
                "title": "",
                "url": f"/in/{public_id}/details/{slug}/",
                "inheritActor": False,
                "colorScheme": "ColorScheme_UNKNOWN",
                "disableScreenGutters": False,
                "shouldHideMobileTopNavBar": False,
                "shouldHideLoadingSpinner": False,
                "replaceCurrentScreen": False,
                "shouldHideMobileTopNavBarDivider": False,
                "clearBackStack": False,
                "newHierarchy": {
                    "$type": "proto.sdui.navigation.ScreenHierarchy",
                    "screenHash": "com.linkedin.sdui.flagshipnav.home.Home#0",
                    "screenId": "com.linkedin.sdui.flagshipnav.home.Home",
                    "pageKey": "",
                    "isAnchorPage": True,
                    "url": "",
                    "childHierarchy": {
                        "$type": "proto.sdui.navigation.ScreenHierarchy",
                        "screenHash": f"{screen_id}#0",
                        "screenId": screen_id,
                        "pageKey": "",
                        "isAnchorPage": True,
                        "url": "",
                    },
                },
                "screenTitle": "",
                "requestedArguments": {
                    "payload": {"vanityName": public_id},
                    "states": [],
                    "requestMetadata": {
                        "$type": "proto.sdui.common.RequestMetadata"
                    },
                    "screenId": screen_id,
                    "knownTemplateIds": [],
                },
            },
            separators=(",", ":"),
        )
        return self._request("POST", url, public_id, body=body)

    def fetch_detail_page(
        self, public_id: str, profile_id: str, section: str, start: int = 0
    ) -> str:
        """Fetch one page of a section's full entry list."""
        pager, screen, extra = DETAIL_PAGERS[section]
        pager_id = f"com.linkedin.sdui.pagers.profile.details.{pager}"
        screen_id = f"com.linkedin.sdui.flagshipnav.profile.{screen}"
        payload = {
            "vanityName": public_id,
            "profileId": profile_id,
            "start": start,
            "count": PAGE_SIZE,
            **extra,
        }
        arguments = {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": payload,
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
        }
        body = json.dumps(
            {
                "pagerId": pager_id,
                "clientArguments": {
                    **arguments,
                    "states": [],
                    "screenId": screen_id,
                    "knownTemplateIds": [],
                },
                "paginationRequest": {
                    "$type": "proto.sdui.actions.requests.PaginationRequest",
                    "pagerId": pager_id,
                    "trigger": {
                        "$case": "itemDistanceTrigger",
                        "itemDistanceTrigger": {
                            "$type": "proto.sdui.actions.requests."
                                     "ItemDistanceTrigger",
                            "preloadDistance": 3,
                            "preloadLength": 250,
                        },
                    },
                    "retryCount": 2,
                    "requestedArguments": arguments,
                },
            },
            separators=(",", ":"),
        )
        # Pagers use a distinct action endpoint. Posting this body to
        # /actions/component returns 500.
        url = (
            f"{BASE}/flagship-web/rsc-action/actions/pagination"
            f"?sduiid={pager_id}"
        )
        slug = DETAIL_SCREENS[section][0]
        return self._request(
            "POST", url, public_id, body=body,
            referer=f"{BASE}/in/{public_id}/details/{slug}/",
        )

    def fetch_all_entries(
        self, public_id: str, profile_id: str, section: str
    ) -> list[TextNode]:
        """Page through a section until it is exhausted.

        Stops when a page yields no new text, or at :data:`MAX_PAGES`. The
        no-new-text check is what actually terminates: the pager keeps returning
        200 past the end of the list rather than signalling completion.
        """
        collected: list[TextNode] = []
        seen: set[str] = set()

        for page in range(MAX_PAGES):
            body = self.fetch_detail_page(
                public_id, profile_id, section, start=page * PAGE_SIZE
            )
            nodes = list(iter_text(parse_flight(body)))

            # A page past the end renders empty-state copy rather than entries.
            if any(n.text.startswith("Nothing to see for now") for n in nodes):
                break

            fresh = [n for n in nodes if n.text not in seen]
            if not fresh:
                break
            seen.update(n.text for n in fresh)
            collected.extend(nodes)
            if len(nodes) < PAGE_SIZE:
                break

        return collected

    def fetch_identity(self, public_id: str) -> dict:
        """Fetch the Voyager Dash identity record.

        Supplies fields the SDUI payload does not carry at all: locale variants,
        image expiry, an authoritative first/last split, industry and websites.
        Best-effort — a failure here costs those fields, not the profile.
        """
        url = DASH_PROFILES.format(public_id=public_id)
        headers = {
            "Host": "www.linkedin.com",
            "csrf-token": self.settings.csrf_token,
            "x-restli-protocol-version": "2.0.0",
            # Returns a flat included[] graph rather than deep nesting.
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "x-li-lang": "en_US",
            "user-agent": USER_AGENT,
            "referer": f"{BASE}/in/{public_id}/",
        }
        body = self._request("GET", url, public_id, headers_override=headers)
        return parse_identity(json.loads(body))

    # -- orchestration ----------------------------------------------------

    def fetch_profile(
        self,
        public_id: str,
        sections: Sequence[str] = DEFAULT_SECTIONS,
        *,
        complete: bool = False,
    ) -> FetchResult:
        """Fetch the shell plus whichever components the sections require.

        A component that fails is recorded in ``warnings`` and skipped; the rest
        of the profile is still returned, which is what makes a 206 possible
        instead of an all-or-nothing failure.
        """
        result = FetchResult()

        # The screen response intermittently omits the top card entirely,
        # returning 200 either way (METHODOLOGY §13). Since it carries name,
        # headline, location and follower count, one retry is worth far more
        # than the request it costs — measured at roughly a third of fetches
        # in the coverage survey.
        shell, profile_id = self.fetch_screen(public_id)
        nodes = list(iter_text(parse_flight(shell)))
        if not _has_top_card(nodes):
            log.info("top card missing for %s; retrying the screen", public_id)
            self._backoff(1)
            retry_shell, retry_id = self.fetch_screen(public_id)
            retry_nodes = list(iter_text(parse_flight(retry_shell)))
            if _has_top_card(retry_nodes):
                shell, profile_id, nodes = retry_shell, retry_id or profile_id, retry_nodes
            else:
                result.warnings.append(
                    "top card absent from two consecutive screen responses"
                )

        result.profile_id = profile_id
        result.nodes.extend(nodes)
        result.components_used.append("screen")

        if profile_id is None:
            result.warnings.append(
                "could not determine the durable profile id; "
                "only the top card was fetched"
            )
            return result

        wanted = sorted(
            {SECTION_COMPONENTS[s] for s in sections if s in SECTION_COMPONENTS}
        )
        for name in wanted:
            try:
                body = self.fetch_component(public_id, profile_id, name)
            except LinkedInError as exc:
                log.warning("component %s failed: %s", name, exc)
                result.warnings.append(f"section component {name} failed: {exc}")
                continue
            result.nodes.extend(iter_text(parse_flight(body)))
            result.components_used.append(name)

        if complete:
            self._replace_with_full_lists(public_id, profile_id, sections, result)

        # Identity last: it is supplementary, and a failure here must not cost
        # the sections already fetched.
        try:
            result.identity = self.fetch_identity(public_id)
        except LinkedInError as exc:
            log.info("identity record unavailable for %s: %s", public_id, exc)
            result.warnings.append(
                f"Voyager identity record unavailable ({exc}); locale variants, "
                "image expiry and the exact name split are omitted"
            )

        return result

    def _replace_with_full_lists(
        self,
        public_id: str,
        profile_id: str,
        sections: Sequence[str],
        result: FetchResult,
    ) -> None:
        """Swap truncated card entries for the pager's complete list.

        Costly — one or more extra requests per section — so it is opt-in. A
        section whose pager fails keeps the card's truncated entries, which are
        still correct as far as they go, and records a warning.
        """
        for section in sections:
            if section not in DETAIL_PAGERS:
                continue
            label = PAGER_SECTION_LABELS[section]
            try:
                entries = self.fetch_all_entries(public_id, profile_id, section)
            except LinkedInError as exc:
                log.warning("full %s list failed: %s", section, exc)
                result.warnings.append(
                    f"could not fetch the complete {section} list ({exc}); "
                    "returning the truncated card entries"
                )
                continue

            if not entries:
                continue

            # Drop the card's version so entries are not duplicated.
            result.nodes = [n for n in result.nodes if n.section != label]
            result.nodes.extend(replace(node, section=label) for node in entries)
            result.components_used.append(f"pager:{section}")
