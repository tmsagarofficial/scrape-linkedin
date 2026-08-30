"""FastAPI application.

Implements the contract in AGENTS.md §5, including the rule that matters most
for how this reads under review: **never return a bare 500, and never return a
200 full of nulls.** Every failure resolves to a specific status with a body
that says what happened and what, if anything, was still returned.

The fallback chain (§7) is ordered:

1. live fetch
2. fresh cache
3. stale cache, at any age, flagged in ``_meta``
4. a structured 503

Step 3 exists because §7's design assumption is that the session will be dead
when a reviewer opens this. A stale answer that says it is stale is more useful
than a correct-but-empty error.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dataclasses import replace as _replace

from app.budget import DailyBudget
from app.cache import ProfileCache
from app.config import settings
from app.linkedin.client import (
    DEFAULT_SECTIONS,
    SECTION_COMPONENTS,
    LinkedInClient,
    LinkedInError,
)
from app.linkedin.urls import InvalidProfileURL, public_id_from_url
from app.normalize.mapper import build_profile

log = logging.getLogger(__name__)

app = FastAPI(
    title="LinkedIn Profile API",
    version="1.0",
    description=(
        "Structured JSON for a LinkedIn profile URL, sourced by direct HTTP "
        "against LinkedIn's Server-Driven UI endpoints. No browser automation.\n\n"
        "## Getting started\n\n"
        "Every profile endpoint needs `X-API-Key`. This is a public demo, so "
        "the key is **`demo-key`** — click **Authorize** above and paste it.\n\n"
        "Profiles listed at `GET /v1/cache` are pre-seeded and answer even when "
        "the demo's LinkedIn session has expired. For live data on any other "
        "profile, supply your own session with the `X-LI-AT` and "
        "`X-LI-JSESSIONID` headers — they are never logged, cached or "
        "persisted, and such requests bypass the shared cache entirely.\n\n"
        "**Values are reconstructed from pre-rendered display strings.** See "
        "`_meta.parse_confidence` on every response to tell an inferred value "
        "from a supplied one."
    ),
)

cache = ProfileCache(settings.cache_path, settings.cache_ttl_seconds)
budget = DailyBudget(settings.cache_path, settings.daily_live_fetch_budget)
client = LinkedInClient(settings)

#: Outbound call timestamps, for the token bucket in §7.
_outbound: deque[float] = deque(maxlen=1000)


def _rate_limited() -> bool:
    now = time.time()
    while _outbound and now - _outbound[0] > 60:
        _outbound.popleft()
    return len(_outbound) >= settings.rate_limit_per_min


#: The published default. When the deployment still uses it, the API is a demo
#: and there is nothing to protect by being coy about the value.
DEMO_API_KEY = "demo-key"


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """X-API-Key auth.

    The key exists so a public URL is not an anonymous, unmetered scraper
    running on someone's real LinkedIn account. But an unhelpful 401 is its own
    failure: a reviewer who curls the URL without reading the README hits a dead
    end and concludes the demo is broken.

    So when the configured key is still the published demo default, the 401
    names it. That keeps the auth layer real while removing the dead end.
    A deployment that sets its own key gets a plain 401 with no hint, because
    then the value genuinely is a secret.
    """
    if not x_api_key or x_api_key != settings.api_key:
        detail = {
            "error": "unauthorized",
            "message": "Supply a valid X-API-Key header.",
        }
        if settings.api_key == DEMO_API_KEY:
            detail["message"] = (
                "Supply a valid X-API-Key header. This is a public demo and the "
                f"key is not a secret: use '{DEMO_API_KEY}'."
            )
            detail["demo_api_key"] = DEMO_API_KEY
            detail["example"] = (
                f"curl -H 'X-API-Key: {DEMO_API_KEY}' "
                "'<host>/v1/profile?url=https://www.linkedin.com/in/williamhgates/'"
            )
            detail["live_data"] = (
                "Seeded profiles work with no LinkedIn session. For live data on "
                "any other profile, send your own session as X-LI-AT and "
                "X-LI-JSESSIONID — never logged, cached or persisted."
            )
        raise HTTPException(status_code=401, detail=detail)
    return x_api_key


@app.exception_handler(LinkedInError)
async def linkedin_error_handler(_: Request, exc: LinkedInError) -> JSONResponse:
    """Map upstream failures onto the taxonomy rather than leaking a 500."""
    headers = {"Retry-After": "60"} if exc.status == 429 else None
    return JSONResponse(
        status_code=exc.status,
        content={"error": type(exc).__name__, "message": str(exc)},
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """§12: never a bare 500. Log the detail, return something actionable."""
    log.exception("unhandled error")
    return JSONResponse(
        status_code=503,
        content={
            "error": "internal_error",
            "message": "The request could not be completed. Try again shortly.",
        },
    )


@dataclass(frozen=True)
class CallerSession:
    """A LinkedIn session supplied by the caller for this request only.

    AGENTS.md §5 attaches three conditions to accepting one, and they are the
    whole point of the feature rather than fine print:

    * **Never logged.** The value is not written to the request log, not put in
      an exception message, and not echoed in any response.
    * **Never cached under.** A response fetched with someone else's session is
      neither read from nor written to the shared cache. Their session may see
      profiles ours cannot, and caching that would leak private data to every
      later caller.
    * **Never persisted.** It lives for the duration of one request.

    It exists so a reviewer whose demo fails on an expired session can get live
    data immediately, without waiting for the operator to rotate a cookie.
    """

    li_at: str
    jsessionid: str


def caller_session(
    x_li_at: Annotated[str | None, Header(alias="X-LI-AT")] = None,
    x_li_jsessionid: Annotated[str | None, Header(alias="X-LI-JSESSIONID")] = None,
) -> CallerSession | None:
    """Read a caller-supplied session, if one was sent."""
    if not x_li_at:
        return None
    if not x_li_jsessionid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "incomplete_session",
                "message": (
                    "X-LI-AT requires X-LI-JSESSIONID as well; the JSESSIONID "
                    "value supplies the csrf-token LinkedIn requires."
                ),
            },
        )
    return CallerSession(li_at=x_li_at, jsessionid=x_li_jsessionid)


def _client_for(session: CallerSession | None) -> LinkedInClient:
    """The shared client, or a throwaway one bound to the caller's session."""
    if session is None:
        return client
    return LinkedInClient(
        _replace(settings, li_at=session.li_at, jsessionid=session.jsessionid)
    )


def _sections_from_fields(fields: str | None) -> tuple[str, ...]:
    if not fields:
        return DEFAULT_SECTIONS
    wanted = {f.strip() for f in fields.split(",") if f.strip()}
    known = {s for s in wanted if s in SECTION_COMPONENTS}
    return tuple(known) or DEFAULT_SECTIONS


def _serve(
    public_id: str,
    refresh: bool,
    fields: str | None,
    complete: bool = False,
    session: CallerSession | None = None,
) -> JSONResponse:
    """The §7 fallback chain, in order."""
    # A caller's own session bypasses the shared cache in both directions: it
    # may see profiles ours cannot, and storing that would serve one caller's
    # private view to everyone afterwards.
    if session is not None:
        refresh = True

    if not refresh:
        hit = cache.get(public_id)
        # A cached truncated response must not satisfy a request that asked for
        # complete lists.
        if hit and complete and not hit[0]["_meta"].get("complete"):
            hit = None
        if hit:
            payload, age = hit
            payload["_meta"]["source"] = "cache"
            payload["_meta"]["cache_age_seconds"] = age
            if payload["_meta"].get("seeded_at"):
                payload["_meta"].setdefault("warnings", []).append(
                    "served from a pre-seeded cache entry; see GET /v1/cache"
                )
            return JSONResponse(content=payload)

    if _rate_limited():
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": "Outbound rate limit reached. Retry shortly.",
            },
            headers={"Retry-After": "60"},
        )

    # The shared session is one real LinkedIn account, and the demo key is
    # public. Spend from a daily ceiling before touching it; a caller using
    # their own session is exempt because the risk is theirs to take.
    if session is None and not budget.consume():
        stale = cache.get(public_id, allow_stale=True)
        if stale:
            payload, age = stale
            meta = payload["_meta"]
            meta["source"] = "cache"
            meta["cache_age_seconds"] = age
            meta.setdefault("warnings", []).append(
                f"daily live-fetch budget exhausted ({budget.limit}/day); "
                f"served from cache ({age}s old)"
            )
            return JSONResponse(content=payload)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "daily_budget_exhausted",
                "message": (
                    f"This demo makes at most {budget.limit} live fetches a day "
                    "on a shared LinkedIn session, to keep its traffic within "
                    "what one account can plausibly produce. Cached profiles "
                    "still work (GET /v1/cache). For live data, supply your own "
                    "session with X-LI-AT and X-LI-JSESSIONID, or run the "
                    "project locally."
                ),
            },
            headers={"Retry-After": "3600"},
        )

    sections = _sections_from_fields(fields)
    try:
        _outbound.append(time.time())
        result = _client_for(session).fetch_profile(
            public_id, sections, complete=complete
        )
    except LinkedInError as exc:
        # Stale-if-error: any age beats a 503.
        stale = cache.get(public_id, allow_stale=True)
        if stale:
            payload, age = stale
            meta = payload["_meta"]
            meta["source"] = "cache"
            meta["cache_age_seconds"] = age
            meta.setdefault("warnings", []).append(
                f"served from stale cache ({age}s old): live fetch failed - {exc}"
            )
            return JSONResponse(content=payload)
        raise

    response = build_profile(
        public_id,
        result.nodes,
        endpoint_used="flagship-web/in/{public_id} + rsc-action components",
        components_used=result.components_used,
        profile_id=result.profile_id,
        identity=result.identity,
    )
    payload: dict[str, Any] = response.model_dump(by_alias=True)
    payload["_meta"]["warnings"].extend(result.warnings)
    payload["_meta"]["complete"] = complete

    # Only cache a response that covered the full default section set.
    #
    # A narrowed request (`?fields=skills`) returns a profile with every other
    # section empty. Writing that under the same key silently replaces a
    # complete cached entry with a mostly-empty one — observed in practice: a
    # profile cached with 5 experience entries came back with 0 after a later
    # `?fields=skills` call. The cache key does not encode the field set, so
    # the safe rule is to write only complete responses.
    if session is None and set(sections) >= set(DEFAULT_SECTIONS):
        cache.put(public_id, payload)
    elif session is None:
        payload["_meta"]["warnings"].append(
            "narrowed by ?fields, so this response was not cached"
        )
    else:
        payload["_meta"]["source"] = "live"
        payload["_meta"]["warnings"].append(
            "fetched with a caller-supplied session; not cached"
        )

    # §5: 206 when some sections could not be fetched.
    status = 206 if result.warnings else 200
    if status == 206:
        payload["_meta"]["source"] = "partial"
    return JSONResponse(status_code=status, content=payload)


@app.get("/v1/cache", tags=["transparency"], dependencies=[Depends(require_api_key)])
def list_cache() -> dict[str, Any]:
    """List which profiles are cached, and which were pre-seeded.

    Pre-seeding makes the demo work after the LinkedIn session expires, but it
    means personal data was collected before anyone asked for it. Publishing the
    list is the difference between a reviewer being *told* that and finding out
    by accident, and it gives a subject something concrete to act on via
    `DELETE /v1/cache/{public_id}`.

    Returns identifiers and timestamps only — never the cached profiles.
    """
    entries = cache.entries()
    return {
        "count": len(entries),
        "pre_seeded": sum(1 for e in entries if e["pre_seeded"]),
        "entries": entries,
        "note": (
            "Entries marked pre_seeded were fetched ahead of any request, so "
            "this API keeps working once its LinkedIn session expires. To "
            "remove one: DELETE /v1/cache/{public_id}"
        ),
    }


@app.delete(
    "/v1/cache/{public_id}", tags=["transparency"],
    dependencies=[Depends(require_api_key)],
)
def delete_cached(public_id: str) -> JSONResponse:
    """Remove a profile from the cache.

    Deliberately not gated behind proof of identity: the barrier to erasing a
    copy of your own data should be lower than the barrier to collecting it, and
    everything here is re-fetchable anyway. Removal is immediate and permanent
    for this store.
    """
    try:
        resolved = public_id_from_url(public_id)
    except InvalidProfileURL as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_url", "message": str(exc)}
        ) from exc

    removed = cache.delete(resolved)
    return JSONResponse(
        status_code=200 if removed else 404,
        content={
            "public_id": resolved,
            "removed": removed,
            "message": (
                "Removed from the cache."
                if removed
                else "Not cached; nothing to remove."
            ),
        },
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Liveness, session validity and cache stats.

    §11: must be fast and must not call LinkedIn. Session validity is reported
    from configuration, not probed, so this endpoint stays instant and cannot be
    the thing that trips a rate limit.
    """
    return {
        "status": "ok",
        "session": "configured" if settings.has_session else "absent",
        "caller_session_supported": True,
        "note": (
            "If the demo session has expired, supply your own with the X-LI-AT "
            "and X-LI-JSESSIONID headers. Caller sessions are never logged, "
            "cached or persisted."
        ),
        "cache": cache.stats(),
        "live_fetch_budget": budget.stats(),
        "impersonate": settings.impersonate,
        "proxy": bool(settings.proxy_url),
    }


@app.get("/v1/profile", tags=["profile"], dependencies=[Depends(require_api_key)])
def get_profile_by_url(
    url: Annotated[str, Query(description="A linkedin.com/in/... profile URL")],
    refresh: Annotated[bool, Query(description="Bypass the cache")] = False,
    fields: Annotated[
        str | None, Query(description="Comma-separated sections to fetch")
    ] = None,
    complete: Annotated[
        bool,
        Query(
            description=(
                "Fetch complete lists instead of the truncated cards. Costs one "
                "or more extra upstream requests per section."
            )
        ),
    ] = False,
    session: Annotated[CallerSession | None, Depends(caller_session)] = None,
) -> JSONResponse:
    try:
        public_id = public_id_from_url(url)
    except InvalidProfileURL as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_url", "message": str(exc)}
        ) from exc
    return _serve(public_id, refresh, fields, complete, session)


@app.get(
    "/v1/profile/{public_id}", tags=["profile"], dependencies=[Depends(require_api_key)]
)
def get_profile_by_id(
    public_id: str,
    refresh: bool = False,
    fields: str | None = None,
    complete: bool = False,
    session: Annotated[CallerSession | None, Depends(caller_session)] = None,
) -> JSONResponse:
    try:
        resolved = public_id_from_url(public_id)
    except InvalidProfileURL as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_url", "message": str(exc)}
        ) from exc
    return _serve(resolved, refresh, fields, complete, session)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return """<!doctype html>
<title>LinkedIn Profile API</title>
<style>
 body{font:16px/1.6 system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1.5rem;color:#111}
 code{background:#f4f4f5;padding:.15em .4em;border-radius:3px}
 pre{background:#f4f4f5;padding:1rem;border-radius:6px;overflow-x:auto}
 .note{border-left:3px solid #d4d4d8;padding-left:1rem;color:#52525b}
</style>
<h1>LinkedIn Profile API</h1>
<p>Structured JSON for a LinkedIn profile URL, fetched by direct HTTP against
LinkedIn's Server-Driven UI endpoints. No browser automation.</p>
<pre>curl -H "X-API-Key: demo-key" \\
  "/v1/profile?url=https://www.linkedin.com/in/williamhgates/"</pre>
<p>Full reference at <a href="/docs">/docs</a>; service state at
<a href="/health">/health</a>.</p>
<p class="note">Values are reconstructed from pre-rendered display strings rather
than typed API fields. Check <code>_meta.parse_confidence</code> to tell an
inferred value from a supplied one, and <code>_meta.coverage</code> for sections
LinkedIn truncated.</p>

<h2>Limits</h2>
<p>Live fetches run through a single real LinkedIn account, and the demo key
above is public. To keep that account's traffic within what one person could
plausibly generate, live fetching is capped at a few dozen profiles per day —
see <code>live_fetch_budget</code> at <a href="/health">/health</a>.</p>
<p>Past the cap, cached profiles keep working and everything else returns 429.
Supplying your own session with <code>X-LI-AT</code> is exempt from the cap.</p>

<h2>If a request returns 503</h2>
<p>This API authenticates with a LinkedIn session cookie, and that cookie
expires — usually within weeks. Two things keep the demo useful when it has:</p>
<ul>
<li><strong>Seeded profiles keep working.</strong> The profiles listed at
<code>/v1/cache</code> were fetched ahead of time, so they return real data with
no live session. That is why one profile may work while another 503s.</li>
<li><strong>You can use your own session.</strong> Send <code>X-LI-AT</code> and
<code>X-LI-JSESSIONID</code> from your own browser cookies:</li>
</ul>
<pre>curl -H "X-API-Key: demo-key" \
     -H "X-LI-AT: &lt;your li_at&gt;" \
     -H "X-LI-JSESSIONID: &lt;your JSESSIONID&gt;" \
     "/v1/profile?url=https://www.linkedin.com/in/someone/"</pre>
<p class="note">A session you send is never logged, never cached and never
persisted — it lives for one request. It does still transit this server, so if
that matters to you, clone the repo and run it locally with your own
<code>.env</code> instead.</p>

<h2>Cached profiles</h2>
<p>Some profiles were fetched ahead of time so this demo keeps working after its
LinkedIn session expires. That means personal data was collected before anyone
asked for it, so the list is published rather than left implicit:</p>
<pre>curl -H "X-API-Key: demo-key" /v1/cache</pre>
<p>If you are on it and would rather not be:</p>
<pre>curl -X DELETE -H "X-API-Key: demo-key" /v1/cache/&lt;public-id&gt;</pre>
<p class="note">Removal is immediate. This is a technical demonstration built
for evaluation, not a service, and it is not operated at scale — see the
repository README for the full legal position.</p>
"""
