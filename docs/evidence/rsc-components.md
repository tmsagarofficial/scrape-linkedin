# SDUI / RSC component catalogue

Derived from `www.linkedin.com.har` (140 entries, logged-in session loading and
scrolling `https://www.linkedin.com/in/{public_id}/`).

Regenerate with:

```bash
python3 scripts/extract_rsc_from_har.py www.linkedin.com.har
```

## Confirmation that the legacy endpoint is gone

| Check | Result |
|---|---|
| HAR entries total | 140 |
| Requests to `/voyager/api/identity/profiles/*/profileView` | **0** |
| Direct request to that endpoint (`scripts/check_profileview_410.py`) | **410 Gone** |

Two independent signals agree: the browser never calls it, and calling it
directly returns the explicit "permanently removed" status.

**`/voyager/` as a whole is *not* dead**, which is a meaningful distinction the
original spec did not draw. Thirteen `/voyager/api/graphql` calls appear in the
same capture, serving messaging, search typeahead and network recommendations:

```
/voyager/api/graphql?queryId=voyagerMessagingDashAffiliatedMailboxes.aef2238...
/voyager/api/graphql?queryId=voyagerSearchDashTypeahead.fa9acbcb761f7b5ec2...
/voyager/api/voyagerRelationshipsDashCohorts?decorationId=...
```

So the migration to SDUI is **per-surface, not platform-wide**. The profile
surface has moved; messaging and search have not. This bounds the blast radius
of the finding and is worth stating precisely rather than claiming "Voyager is
retired".

## Calls observed

| Entry | Method | Kind | Component / request | Bytes |
|---|---|---|---|---|
| 83 | GET | page | `/flagship-web/in/{public_id}` | 257,857 |
| 89 | POST | component | `profileCardsActivity` | 372,691 |
| 106 | POST | server-request | `fetchProfileDiscoveryDrawer` | 405 |
| 115 | POST | component | `profileCardsBelowActivityPart1` | 55,074 |
| 127 | POST | server-request | `saveProfileToPdf` | 1,493 |
| 131 | POST | component | `profileCardsActivity` (repeat) | 372,613 |
| 136 | POST | server-request | `feedBadgeRequest` | 405 |

## Component name enumeration

Grepping **every request URL and every decoded response body** in the capture
for `com.linkedin.sdui.generated.profile.dsl.impl.*` yields exactly two names:

| Component | Occurrences |
|---|---|
| `profileCardsActivity` | 6 |
| `profileCardsBelowActivityPart1` | 3 |

### `Part2`…`Part7` exist — confirmed by live probe

An earlier draft of this document concluded, from the HAR alone, that there was
no evidence further numbered parts existed. **That conclusion was wrong**, and it
is recorded here rather than quietly edited out, because the way it was wrong is
the useful part: absence of evidence in a capture is not evidence of absence. The
HAR only shows what the browser happened to fetch during that session.

A single authenticated `GET /flagship-web/in/{public_id}` returns the profile
*screen shell*, and the shell **declares the components it will lazily fetch**.
Grepping that one response yields the full catalogue:

| Component | Purpose (inferred from name) |
|---|---|
| `profileCardsAboveActivity` | cards above the activity feed |
| `profileCardsActivity` | recent activity / posts |
| `profileCardsExperienceOnly` | experience in isolation |
| `profileCardsBelowActivityPart1WithoutExp` | Part1 variant excluding experience |
| `profileCardsBelowActivityPart2` … `Part7` | remaining sections |

Six further parts, plus three variants that never appeared in the HAR at all.

This is the more valuable finding: **the entire profile component surface is
enumerable from one request**, with no scrolling, no repeated captures and no
browser. That replaces the "scroll a real profile and re-export a HAR" recon
step the plan assumed was necessary.

The mapping of part number to section (skills, languages, honours) is still
unknown. Each would need one request to identify, and that probing has been
deliberately deferred to keep authenticated request volume low.

## Sections recovered from `profileCardsBelowActivityPart1`

The parser labels sections from LinkedIn's own component metadata rather than
from text position. Running `app/linkedin/rsc_parser.py` over entry 115:

| Section label | Source field | Entries |
|---|---|---|
| `profile-card-experience` | `viewTrackingSpecs.viewName` | 3 companies, 4 roles |
| `profile-card-education` / `education-lockup-view` | `viewTrackingSpecs.viewName` | 1 |
| `profile-card-licenses-and-certifications` | `viewTrackingSpecs.viewName` | 2 |

`profileCardsActivity` (372 KB) yields only two text nodes, under
`profile-card-recent-activity`. It is almost entirely image and post payload,
and carries **no** profile fields worth mapping.

### The top card is not in either component

Name, headline, location and About do **not** appear in `Part1` or in
`profileCardsActivity`. They belong to the profile page shell, fetched as:

```
GET /flagship-web/in/{public_id}?skipRedirect=true&miniProfileUrn=urn:li:fs_miniProfile:{profileId}
```

The HAR retained this request but not its response body, so the top card was
initially unverified. **A live probe has since confirmed it** — see "Live probe"
below. The `miniProfileUrn` turns out to be unnecessary.

Note the shape of that call: it is a **GET** whose only inputs are the public id
and the profile URN. If the top card comes from there, it needs none of the
3 KB `profileComponentState` body the POST component calls require — a
materially simpler client path than the one the plan assumed.

## Where `vieweeProfileId` comes from

The durable id (`ACoAA…`) is present in the entry 83 request URL itself, as
`miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACoAA…`, and again inside the
`Part1` response as part of the card component keys:

```
com.linkedin.sdui.profile.card.ref{profileId}ExperienceTopLevelSection
```

## Header findings

Full header sets are in `rsc-calls.json`. Two points change the client design.

### 1. This capture is the mobile web client, not desktop

```
user-agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N)
            AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 ...
sec-ch-ua-mobile: ?1
sec-ch-ua-platform: "Android"
x-li-anchor-page-key: p_mwlite_profile_view_base
x-li-application-version: 0.2.7003
```

`mwlite` is LinkedIn's mobile web surface. AGENTS.md §1 specifies
`impersonate="chrome124"` (desktop) and §2.3 warns that a user-agent
inconsistent with the impersonated TLS profile *is itself a detection signal*.
Replaying these headers under a desktop `chrome124` fingerprint would ship
exactly that inconsistency: an Android UA over a desktop TLS handshake.

`curl_cffi` 0.16.2 offers `chrome142` (desktop) and `chrome131_android` /
`chrome_android`. Neither is an exact match for Chrome 142 on Android. See
METHODOLOGY for the decision.

### 2. The telemetry ids look client-generated, not server-issued

`x-li-pageforestid` is byte-identical to the trace id inside `x-li-traceparent`,
across both requests:

```
x-li-pageforestid : 00065a2be64edbf4009d0b718c5aeb93
x-li-traceparent  : 00-00065a2be64edbf4009d0b718c5aeb93-0c40f75a6cc2f079-00
```

W3C `traceparent` trace ids are generated by the client. That `pageforestid` is
the same value is strong evidence it is client-minted session telemetry rather
than a server-issued token.

Similarly, `x-li-page-instance` merely embeds `x-li-page-instance-tracking-id`:

```
x-li-page-instance: urn:li:page:p_mwlite_profile_view_base;fGTQg22LTVuXRmBFfzz9SQ==
x-li-page-instance-tracking-id: fGTQg22LTVuXRmBFfzz9SQ==
```

Both are 16 random bytes, base64-encoded — again a client-side construction.

This *suggests* these headers are accepted when well-formed and that no warm-up
request is required. It is not yet proven: the question is only settled by a
live A/B (replayed values vs. freshly generated ones), which needs a valid
session and explicit go-ahead. Recorded here as a hypothesis with its
supporting evidence, not as a conclusion.


## Live probe (build step 4f)

One authenticated request, `scripts/probe_rsc_endpoint.py`, run with a valid
`LI_AT` + `JSESSIONID`:

```
GET https://www.linkedin.com/flagship-web/in/{public_id}
    x-li-rsc-stream: true
    sec-fetch-mode: cors     (a fetch, not a document navigation)
    impersonate=chrome142

-> 200   755,801 bytes   RSC flight stream
```

Three open questions closed by that single request.

### 1. The endpoint serves a non-browser client

200 with a parseable RSC body, from `curl_cffi`. No 999, no login redirect, no
HTML fallback. The response is 755 KB — three times the 257 KB seen in the HAR.

### 2. `miniProfileUrn` is *not* required

The probe sent the vanity slug alone, with no durable `ACoAA…` id. This removes
the bootstrap problem entirely: `/v1/profile?url=` resolves in **one** request,
not a lookup followed by a fetch.

### 3. Telemetry headers are not validated server-side

`x-li-pageforestid`, `x-li-page-instance`, `x-li-page-instance-tracking-id` and
`x-li-traceparent` were **freshly generated**, not replayed from the HAR, and the
request succeeded. Combined with the observation that `pageforestid` equals the
traceparent trace id, this confirms they are client-minted telemetry.

**No warm-up request to `/in/{public_id}/` is needed.** The client can call the
endpoint cold.

### What the screen response contains

Parsed with `app/linkedin/rsc_parser.py` — 81 text nodes, 0 parse errors:

| Section label | Fields |
|---|---|
| `profile-top-card` | name, headline, location, followers, connections, current company, school |
| `profile-sticky-header` | name, headline (duplicated) |
| `global-footer-*`, `profileAd*` | site chrome — filter by section label |

The top card is present and cleanly labelled. Experience, education and
certifications are **not** in the shell; they are lazily fetched via the
component endpoints, which the shell enumerates.

### Wire-format finding: two reference forms

The small component responses use almost exclusively the lazy `"$L<hex>"`
reference form. The full screen response is dominated by the **direct
`"$<hex>"`** form — roughly 1,600 occurrences against 1,085 lazy ones.

A parser handling only `$L` indexes all 336 records, reports **zero errors**, and
extracts **nothing**. This is the failure mode worth calling out in
METHODOLOGY.md: in a self-describing format, a missing reference form does not
announce itself as a parse failure. It silently returns an empty document.

### Extraction rule: structure beats naming

An allowlist of content-bearing props (`children`, `initialContent`, …) was built
from the component response and did not survive the screen response, which also
hangs subtrees off `navItemProps`, `renderedChildScreen`, `renderedContentBefore`,
`renderedToolbar` and others.

The parser now uses a structural invariant instead:

> **Rendered text is always an element of a list.
> Metadata is always a scalar dict value.**

`{"children": ["Amazon"]}` yields text; `{"className": "_02484ad3"}` does not.
This holds across both response shapes and does not need updating when LinkedIn
renames a prop.


## Complete component map (resolved empirically)

Each component was probed once. Every request and response is recorded in
`docs/evidence/request-log.jsonl`.

| Component | Sections it renders | Bytes | This profile |
|---|---|---|---|
| `…BelowActivityPart1` | experience, education, licenses & certifications | 55,074 | populated |
| `…BelowActivityPart2` | **recommendations** | 1,122 | empty |
| `…BelowActivityPart3` | **courses, honors, patents, publications, test scores** | 8,721 | courses only |
| `…BelowActivityPart4` | **languages, organizations** | 10,263 | 4 languages |
| `…BelowActivityPart5` | **interests** | 196,114 | populated |
| `…BelowActivityPart6` | **volunteer causes** | 1,169 | empty |
| `…BelowActivityPart7` | **skills** | 16,817 | populated |

`Part7` is the skills card — the section missing from every earlier capture, and
the last significant gap in schema coverage.

### An empty section still identifies itself

`Part2` and `Part6` render nothing for this profile, yet both still declare what
they *are*:

```
observabilityIdentifier: com.linkedin.sdui.impl.profile.components.recommendationsTopLevelSection
componentKey:            com.linkedin.sdui.profile.card.ref{profileId}RecommendationsTopLevel
initialContent:          "$undefined"      <- no content
```

Two consequences:

* The component map is discoverable from any profile, including sparse ones. It
  does not require finding a profile that happens to populate every section.
* **An empty section is distinguishable from a failed fetch.** A card that
  returns 200 with `initialContent: "$undefined"` genuinely has no data, which
  belongs in the schema as an empty array — not as a `partial_fields` warning.
  AGENTS.md §12 forbids fabricating data to fill gaps; this is the signal that
  tells the two cases apart.

### Truncation signals

Cards render a capped number of entries and advertise the true total in a
"see all" control:

| Observed | Shown | Actual |
|---|---|---|
| `Show all 6 licenses` | 2 | 6 |
| `Show all 4 languages` | 2 | 4 |
| `Show all` (skills, experience) | 2 / 4 | unstated |

AGENTS.md §2.7 requires detecting truncation rather than silently returning a
partial list. These strings are that signal, and where the count is given it is
exact. Where the control reads only "Show all", truncation is known but the total
is not — which must be reported as such, not guessed.

### Request body is reconstructible from the vanity name

The 3 KB body is not opaque. `profileComponentState` holds `BindingImpl` entries
whose keys follow a fixed template:

```
ProfileComponentState{Name}{vanityName}ProfileComponentState
```

`scripts/probe_component.py` rebuilds the body from the vanity slug plus the
durable profile id, and the reconstruction is accepted. Only four of the
thirteen observed state keys were included and the requests still returned 200,
so the body is tolerant of omissions.

The durable `ACoAA…` id is recoverable from the screen-shell response, so the
full flow needs no values copied from a browser session:

```
GET  /flagship-web/in/{vanity}          -> top card + component list + profile id
POST /rsc-action/actions/component      -> one call per section needed
```
