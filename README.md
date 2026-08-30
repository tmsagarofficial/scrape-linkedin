# LinkedIn Profile API

Give it a LinkedIn profile URL, get structured JSON back.

Built by reverse-engineering LinkedIn's private endpoints and speaking to them
directly over HTTP. **No browser, no Selenium, no Playwright, no headless
Chrome.**

```bash
curl -H "X-API-Key: demo-key" \
  "https://<host>/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

---

## What you get

```json
{
  "schema_version": "1.0",
  "profile": {
    "public_id": "jordan-rivera",
    "profile_url": "https://www.linkedin.com/in/jordan-rivera/",
    "profile_id": "ACoAAB0000EXAMPLEPROFILEID00000000000",
    "profile_urn": "urn:li:fsd_profile:ACoAAB0000EXAMPLEPROFILEID00000000000",
    "name": { "first": "Jordan", "last": "Rivera", "full": "Jordan Rivera" },
    "headline": "Staff Engineer at Globex | Distributed Systems",
    "location": { "raw": "Springfield, Ohio, United States" },
    "follower_count": 1284,
    "connection_count": "500+ connections",
    "experience": [
      {
        "title": "Staff Engineer",
        "company": {
          "name": "Globex",
          "public_id": "1000001",
          "url": "https://www.linkedin.com/company/1000001/"
        },
        "employment_type": "full_time",
        "location": "Springfield, Ohio, United States",
        "work_mode": "on_site",
        "start": { "year": 2025, "month": 8 },
        "end": null,
        "is_current": true,
        "duration_months": 13
      }
    ],
    "education": [
      {
        "school": "Northgate Institute of Technology",
        "school_url": "https://www.linkedin.com/school/2000001/",
        "degree": "Bachelor of Engineering - BE",
        "field": "Electrical, Electronics and Communications Engineering",
        "start": { "year": 2018, "month": null },
        "end": { "year": 2022, "month": null }
      }
    ],
    "skills": [{ "name": "Machine Learning", "endorsement_count": 3 }],
    "certifications": [
      {
        "name": "Data Science Professional Certificate",
        "authority": "OpenCourse",
        "issued": { "year": 2020, "month": 8 }
      }
    ],
    "languages": [{ "name": "English" }],
    "courses": [{ "name": "Advanced Android App Development" }]
  },
  "_meta": {
    "source": "live",
    "data_layer": "rsc_parsed",
    "fetched_at": "2026-08-29T12:14:02Z",
    "coverage": {
      "profile-card-licenses-and-certifications": {
        "returned": 2, "total": 6, "truncated": true
      }
    },
    "parse_confidence": { "experience[0].duration_months": "parsed" },
    "warnings": ["certifications: LinkedIn returned 2 of 6 entries"]
  }
}
```

### Read `_meta` before you trust a field

Three things there are not decoration:

* **`parse_confidence`** — `"parsed"` means the value was recovered by regex
  from a display string like `"Full-time · 4 yrs 1 mo"`, not read from a typed
  field. LinkedIn no longer serves typed dates on this surface. See
  [Known limitations](#known-limitations).
* **`coverage`** — LinkedIn caps how many entries a card renders. Where it
  states a total (`"Show all 6 licenses"`) it is recorded exactly; where it
  says only `"Show all"`, `total` is `null` and the warning says "unstated".
  It is never guessed.
* **`source`** — `live`, `cache`, or `partial`. A `cache` response carries
  `cache_age_seconds`, and a stale one says so in `warnings`.

Entity ids are numeric rather than vanity slugs, because that is what this data
layer exposes. This costs you nothing: LinkedIn redirects the numeric form to
the readable one (`/company/1586/` → `/company/amazon/`), so follow `url`
rather than trying to rebuild it.

---

## Quickstart

```bash
git clone <repo> && cd linkedin-profile-api
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # add LI_AT and JSESSIONID
uvicorn app.main:app --reload
```

Then open <http://localhost:8000/docs>.

### Getting `LI_AT` and `JSESSIONID`

Log into LinkedIn in a browser, open DevTools → Application → Cookies →
`https://www.linkedin.com`, and copy the `li_at` and `JSESSIONID` values.

This is not a workaround for laziness. LinkedIn's login flow issues a
**JavaScript challenge** that cannot be solved without a browser, and browsers
are out of scope here. Supplying the session cookie directly is the documented,
known-correct answer — the login flow is implemented as the primary path, with
`LI_AT` as an override that bypasses it.

```bash
pytest        # 180 tests, no network, no credentials needed
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, session state, cache stats |
| GET | `/v1/profile?url=…` | Fetch by profile URL |
| GET | `/v1/profile/{public_id}` | Fetch by vanity slug |
| GET | `/v1/cache` | List cached profiles and which were pre-seeded |
| DELETE | `/v1/cache/{public_id}` | Remove a profile from the cache |
| GET | `/docs` | OpenAPI reference |
| GET | `/` | Demo page |

**Auth:** `X-API-Key` header. The demo key is `demo-key`, published here on
purpose — a reviewer who hits 401 with no key may just close the tab.

Because the key is public, it is **not a security boundary**. What protects the
underlying LinkedIn account is the daily budget below, not the key.

### Protecting the shared session

Every live fetch on the demo runs through one real LinkedIn account. A published
key plus an unmetered API is how that account gets restricted, so live fetching
is capped:

| Control | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_PER_MIN` | 3 | Smooths bursts — the binding constraint in practice |
| `DAILY_LIVE_FETCH_BUDGET` | 20 profiles | Keeps daily volume within what one person plausibly browses |

A per-minute limit alone is not enough — 20/min is still ~28,800/day, which
looks nothing like human browsing. Roughly six upstream requests per profile
puts 20 profiles at ~120 requests/day.

Both numbers were lowered after a session-wide soft block was triggered during
development: 300+ requests spread over days caused no problem, then a burst
inside six minutes did. **Rate matters more than total.**

When the budget is spent:

* **cached profiles keep working** — they cost LinkedIn nothing
* **stale cache is served** in preference to an error, and says so
* otherwise **429** with a message naming the alternatives

`GET /health` publishes `live_fetch_budget` so you can see what is left.

**Callers who supply their own session via `X-LI-AT` are exempt** — their
credential, their account, their risk. That is the intended path for anyone who
wants more than a look.

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `url` | — | A `linkedin.com/in/…` URL |
| `refresh` | `false` | Bypass the cache and fetch live |
| `fields` | all core sections | Comma-separated sections to fetch |
| `complete` | `false` | Fetch full lists instead of LinkedIn's truncated cards |

`fields` is a real cost control, not decoration. Each section maps to a separate
upstream request, so `?fields=experience` costs **2** requests where the default
costs **6**.

Valid values: `experience`, `education`, `certifications`, `languages`,
`skills`, `courses`, `honors`, `about`, `recommendations`, `volunteer_causes`.

### `complete` — full lists vs. cards

LinkedIn's profile cards render only the first few entries of each section.
`?complete=true` pages the underlying detail feeds instead:

| | skills | certifications |
|---|---|---|
| default (cards) | 2 | 2 |
| `?complete=true` | **26** | **7** |

It costs extra upstream requests — one per page, per section — so it is off by
default. `_meta.complete` records which mode produced a response, and a cached
card response is never used to satisfy a `complete` request.

### Status codes

| Code | Meaning |
|---|---|
| 200 | OK |
| 206 | Partial — some sections failed; see `_meta.warnings` |
| 400 | Not a LinkedIn member profile URL |
| 401 | Missing or invalid API key |
| 403 | Profile exists but is not visible to the session |
| 404 | No such profile |
| 429 | Rate limited; honour `Retry-After` |
| 503 | Session expired or upstream blocked, and no cached copy |

There is no bare 500. An unexpected error still returns a typed 503 body.

---

## How it works

```
GET /v1/profile
      │
      ├─ parse URL → public_id           400 if it is not a member profile
      ├─ cache hit? ────────────────────► return, _meta.source="cache"
      ▼ miss
   LinkedIn client  (curl_cffi, Chrome TLS impersonation)
      ├─ GET  /flagship-web/in/{public_id}      → top card + durable id
      └─ POST /rsc-action/actions/component ×N  → experience, skills, …
      ▼
   RSC flight parser  → ordered text nodes, tagged by section
      ▼
   Mapper             → public schema + _meta
      ▼
   Cache + response
```

Two details matter more than the rest.

**`curl_cffi` is not interchangeable.** Plain `requests` / `httpx` / `aiohttp`
present a TLS fingerprint no real browser produces, and LinkedIn answers with
HTTP 999 regardless of a valid session cookie. Chrome impersonation is what
makes the identical request succeed.

**The responses are not JSON.** LinkedIn retired the old REST profile endpoint —
it returns **410 Gone** — and replaced it with a Server-Driven UI that speaks
React Server Components over the wire. Parsing that protocol is the bulk of the
work here, and it is written up in **[METHODOLOGY.md](METHODOLOGY.md)**.

---

## Known limitations

Stated plainly, because they affect how much you should trust the output.

**Values are reconstructed from display strings.** The old API returned
`{"startDate": {"month": 8, "year": 2022}}`. This one returns
`"Aug 2022 - Jul 2025 · 3 yrs"`. Employment type, duration, work mode and dates
are recovered by regex from prose. That is lossier than a typed field, and
`_meta.parse_confidence` marks every affected value `"parsed"`.

**English locale only.** The parsers assume English month abbreviations and type
labels. A profile whose `defaultLocale` differs will yield strings they do not
match. Each such field degrades to `null` with a warning rather than raising.

**Entry segmentation is a heuristic validated against two profiles.** LinkedIn
renders several experience layouts and the parser distinguishes them by counting
date ranges. The second profile broke the first version of this logic in four
ways, each now covered by a regression test — a third profile would plausibly
find more. Anything the parser cannot place is reported in `_meta.warnings`
rather than dropped silently.

**Not yet mapped:** the About section, job and school descriptions,
open-to-work and hiring badges, connection degree, and mutual connections.
Company industry and website live on the company page, which is a separate
fetch.

**Never available from LinkedIn:** professional email. Tools that return it use
a separate enrichment vendor that guesses and SMTP-verifies addresses. That is a
different product, not a scraping gap.

**Sections are truncated by LinkedIn** unless you pass `?complete=true`. The
cards return the first few entries; `_meta.coverage` always reports what came
back and, where LinkedIn states it, the true total.

**This is a moving target.** Component ids are generated from LinkedIn's build
and the client version is pinned at `0.2.7003`. Both will drift. The parser is
designed to survive prop renames, but not a protocol change.

**Sessions expire.** `LI_AT` typically lasts weeks. When it dies the API serves
from cache and says so — it does not start returning wrong data.

---

## Cost model

A full profile is **8 upstream requests**: one screen shell plus seven section
components. The default fetch uses **6**. With `?fields=experience`, **2**.

Interests (`Part5`) is ~196 KB, roughly 70% of the total payload, and maps to no
schema field. It is excluded by default.

Caching is therefore the main lever: the default TTL is 24 hours, and a cache
hit costs zero upstream requests. At 20 requests/minute outbound (the default
cap) the ceiling is roughly 150 fresh profiles per hour, before proxy costs.

Residential proxy bandwidth is the real expense. Budget by payload: ~250 KB per
full profile, ~60 KB with `?fields=experience`.

---

## Auditability

Every outbound request is appended to `docs/evidence/request-log.jsonl` — method,
URL, redacted headers, status, response size and classification. Credentials
appear only as truncated SHA-256 fingerprints: enough to tell whether two
requests shared a session, useless for anything else.

The API uses the same logger in production as the research scripts did, so the
audit trail is continuous rather than stopping when development ended.

```bash
python -m app.linkedin.request_log     # summary table
```

Total requests made against LinkedIn during development: **9**.

---

## Deployment

See **[DEPLOYMENT.md](DEPLOYMENT.md)**. The image is provider-neutral — Fly.io,
Render, Railway, Cloud Run or a plain VPS.

The thing that decides success is not the provider but **egress IP quality**.
LinkedIn blocks datacenter ASNs, so a container on any mainstream PaaS will
usually fail on its own IP no matter how valid the cookie. Route LinkedIn
traffic through a residential proxy via `PROXY_URL`.

---

## If the demo returns 503

The API authenticates with a LinkedIn session cookie, and **that cookie expires
— typically within weeks.** Nothing in this design prevents that; it is a
property of the platform. Two things exist so an expired session does not mean
a broken demo.

### 1. Some profiles are pre-seeded

The profiles in `seeds.txt` were fetched ahead of time and cached, so they
return real data whether or not the session is alive. `_meta.source` will read
`cache`, and a pre-seeded response says so in `_meta.warnings`.

```bash
curl -H "X-API-Key: demo-key" https://<host>/v1/cache   # what is seeded
```

This is why an arbitrary profile may 503 while `williamhgates` still works: the
first needs a live session, the second does not.

### 2. You can supply your own session

If you want live data and ours has expired, send your own LinkedIn cookies:

```bash
curl -H "X-API-Key: demo-key" \
     -H "X-LI-AT: <your li_at>" \
     -H "X-LI-JSESSIONID: <your JSESSIONID>" \
     "https://<host>/v1/profile?url=https://www.linkedin.com/in/someone/"
```

Get both from DevTools → Application → Cookies → `https://www.linkedin.com`.

**What happens to a session you send:**

| | |
|---|---|
| Logged | **No.** The request log stores a truncated SHA-256 fingerprint, never the value. |
| Cached | **No.** Your session may see profiles ours cannot; caching that would serve your private view to every later caller. |
| Persisted | **No.** It exists for the duration of one request. |

There are tests asserting each of those three. That said, it does transit a
server you do not control — running locally avoids that entirely, and takes
about a minute.

### 3. Or run it yourself

```bash
git clone <repo> && cd linkedin-profile-api
pip install -e ".[dev]"
cp .env.example .env      # add your own LI_AT and JSESSIONID
uvicorn app.main:app --reload
```

Your cookies stay on your machine. This is the recommended path for anything
beyond a quick look.

## Pre-seeded cache, and how to opt out

Some profiles are fetched **before** anyone requests them, so the API keeps
answering with real data once its LinkedIn session expires — which it will,
typically within weeks.

That is a deliberate trade, and it means personal data was collected without the
subject asking. Rather than leave that implicit, it is exposed at the API:

```bash
curl -H "X-API-Key: demo-key" https://<host>/v1/cache
```

```json
{
  "count": 6,
  "pre_seeded": 6,
  "entries": [{"public_id": "...", "pre_seeded": true, "seeded_at": "..."}]
}
```

The listing returns identifiers and timestamps only — never the cached
profiles, so transparency does not itself become a bulk disclosure. Any response
served from a pre-seeded entry says so in `_meta.warnings`.

To remove one:

```bash
curl -X DELETE -H "X-API-Key: demo-key" https://<host>/v1/cache/<public-id>
```

Removal is immediate and needs no proof of identity. The barrier to erasing a
copy of your own data should be lower than the barrier to collecting it, and
everything here is re-fetchable anyway.

`seeds.txt` lists exactly which profiles are seeded and is committed. It holds
three widely-known public figures and three profiles used for validation.

## Legal position

**This violates LinkedIn's User Agreement.** Not a grey area, and not hedged
here.

LinkedIn litigates aggressively. Proxycurl — a funded business built on exactly
this model — shut down in 2025, citing LinkedIn's legal resources, ending in a
permanent injunction requiring deletion of all scraped data. The reference
open-source client (`tomquirk/linkedin-api`) has since gone private and its
forks are unmaintained.

The relevant case law is unsettled rather than favourable. *hiQ v. LinkedIn*
established that scraping **public** data is not a CFAA violation, but this API
authenticates with a member session, which puts it under the User Agreement as
contract rather than under the CFAA. hiQ ultimately lost on breach of contract.

There are also data-protection obligations independent of LinkedIn. Profiles of
EU or UK residents are personal data under GDPR: a lawful basis is required, and
subjects retain access and erasure rights that a scraped copy makes hard to
honour. `GET /v1/cache` and `DELETE /v1/cache/{public_id}` are a partial,
good-faith answer to the second of those — not a compliance claim.

This project was built for technical evaluation, run against a handful of
profiles, and is not operated at scale. No scraped personal data is committed to
this repository — see `.gitignore`, which excludes the cache, HAR captures, raw
response bodies and the request log. The single committed test fixture has had
its identity replaced with a synthetic one.

Do not deploy this commercially without legal advice.
