# Progress

Tracks the build order in AGENTS.md §13, as amended by the RSC kickoff. Updated
as steps close.

**Status: 11 of 12 steps done. Only deployment remains.**

---

## Build order

| # | Task | Gate | Status |
|---|---|---|---|
| 1 | `curl_cffi` client + full header set + `Host` | One raw profile response saved | ✅ Done |
| 2 | Capture 999-vs-200 TLS evidence | Logs saved for METHODOLOGY | ✅ Done — result contradicts the premise |
| 3 | Session manager + `LI_AT` override | Survives restart without re-auth | ✅ Done |
| 4 | Response parser (RSC, replaces `urn_graph`) | Ordering test passes on fixture | ✅ Done |
| 5 | `mapper.py` + Pydantic schema | Full profile → valid schema, no URNs | ✅ Done |
| 6 | FastAPI + cache + error taxonomy | All error codes reachable locally | ✅ Done |
| 7 | Proxy + public deploy | **Live public URL returns JSON** | ⬜ Not started |
| 8 | Seed cache | Works with session forcibly disabled | ✅ Done, verified |
| 9 | Fixtures + CI | Green with no secrets | ✅ Done |
| 10 | Coverage report | `COVERAGE.md` generated | ✅ Done |
| 11 | README + METHODOLOGY | Written last, not rushed | ✅ Done |
| 12 | Secret scan | `git log -p` grepped clean | ✅ Automated in CI |

### Step 2 is blocked, not skipped

`scripts/check_tls_fingerprint.py` targets the legacy `profileView` endpoint, which
now returns **410 to every client**. Running it produces 410 for both plain
`requests` and `curl_cffi` and demonstrates nothing about TLS fingerprinting.

The comparison must be retargeted at a live endpoint —
`GET /flagship-web/in/{public_id}` with `x-li-rsc-stream: true` — issued twice
with identical cookies. Two requests. This is a graded artifact and should not
ship missing.

---

## Data coverage

Every component in LinkedIn's profile catalogue is now either mapped or
documented with a specific reason.

| Section | Component | Status |
|---|---|---|
| Identity (name split, locales, images, industry, websites) | Voyager Dash | ✅ |
| Top card (name, headline, location, followers) | screen shell | ✅ |
| About | `AboveActivity` | ✅ multi-paragraph |
| Experience | `Part1` | ✅ grouped + single layouts |
| Education | `Part1` | ✅ multiple entries |
| Certifications | `Part1` | ✅ + credential id and URL |
| Recommendations | `Part2` | ✅ counts **and** bodies |
| Courses | `Part3` | ✅ |
| Honors & awards | `Part3` | ✅ |
| Languages | `Part4` | ✅ |
| Interests | `Part5` | ⬜ Excluded — 196 KB, no schema field |
| Volunteer causes | `Part6` | ✅ |
| Skills | `Part7` | ✅ + endorsements and credentials |

**Not mapped, and why:** job/school descriptions, open-to-work and hiring
badges, connection degree, mutual connections (not observed in any capture);
company industry and website (live on the company page — a separate fetch);
professional email (never exposed by LinkedIn at all).

**Two data sources, used where each is better.** Identity fields come from the
Voyager Dash record because they are typed and carry locale variants and image
expiry that the SDUI payload does not express. Sections come from SDUI because
that is the only place they exist — Voyager returns URN pointers for them, not
content. See METHODOLOGY §20.

---

## Verification

| Check | State |
|---|---|
| Tests | 221, offline, no credentials, network-blocked by conftest |
| Cache fallback | 6 profiles served with the session disabled |
| Profiles validated against | 6 (surveyed live, `COVERAGE.md`) |
| Independent oracle | LinkedIn's own PDF export (§11) |
| Outbound requests logged | `docs/evidence/request-log.jsonl` |
| Total requests made in development | ~230, all logged |

### Known parsing limitations

Recorded honestly rather than closed off. See METHODOLOGY for detail.

* ~~**Truncation.**~~ **Solved** via `?complete=true`, which pages the detail
  feeds (`/rsc-action/actions/pagination`) instead of reading the capped cards:
  skills 2 → 26, certifications 2 → 7. Off by default because it costs extra
  requests per section.
* **Entry boundaries differ per card** — navigation URL for experience, font
  weight for skills, `componentKey` for honours, one-node lookahead for
  recommendations. There is no single rule.
* **Presentation styling is load-bearing in one place.** Skills are separated
  from their supporting credentials by `fontWeight`. This will break if
  LinkedIn restyles that card.
* **English locale only** for the section parsers. Non-English profiles degrade
  field-by-field to null with a warning rather than failing. Identity fields are
  now locale-aware via `multiLocale*`, so names and headlines survive where
  section text does not.
* **Burst rate, not daily total, is the binding constraint.** 300+ requests over
  several days caused no problem; a burst inside six minutes triggered a
  session-wide soft block. Defaults lowered to 3/min and 20 profiles/day.
* **LinkedIn's screen response is non-deterministic** — the top card is
  intermittently absent. The client now retries once, and a second failure is
  reported via `_meta.partial_fields` rather than hidden.
* **`courses` and `volunteer_causes` are 0% across the survey.** Both parse
  correctly against captures that contain them, so this is a property of the
  sample, not evidence they work. They remain the least-exercised mappings.

---

## Next

1. ~~**Seed cache**~~ — done. Six profiles, verified serving with `LI_AT`
   unset. An unseeded profile still returns a structured 503 rather than empty
   data.
2. **Seed the client's own team** (AGENTS.md §9 tier 1) — optional; needs
   profile URLs. See the note below on whether it is a good idea.
3. **TLS evidence** — 2 requests, retargeted script.
4. **Deploy** — needs a provider choice and a residential proxy. See
   `DEPLOYMENT.md`; egress IP quality decides this, not the host.

Deployment is the only hard gate that cannot be closed without you.


---

## Note on seeding the client's own team

AGENTS.md §9 puts "the client's own team members" first among seed tiers, on the
reasoning that a reviewer's first instinct is to look themselves up, and a cache
hit makes that instant.

It works, and it is a judgement call rather than a clear win:

* **For:** the demo is fastest and most reliable on exactly the profiles a
  reviewer is most likely to try, and it does not depend on the session still
  being alive weeks later.
* **Against:** it means scraping the reviewer's colleagues before being asked
  to. A reviewer who notices their own data was pre-collected may read that as
  presumptuous rather than thorough, and it is their personal data either way.

The middle option is now implemented, and it is stronger than a README note
because a hosted API is where someone actually encounters it:

* `GET /v1/cache` lists what is stored and which entries were pre-seeded —
  identifiers and timestamps only, never the profile bodies.
* `DELETE /v1/cache/{public_id}` removes one, with no proof of identity
  required.
* Any response served from a pre-seeded entry says so in `_meta.warnings`.
* The demo page at `/` states it in plain language rather than burying it.

With that in place, seeding the client's team becomes defensible: they can see
they are listed and remove themselves in one request. Still their call.

Not seeded currently. `seeds.txt` holds three canonical public figures and three
profiles used for validation.


---

## Handling an expired session

The LinkedIn cookie expires within weeks; a submission is read later than that.
Three mechanisms exist so that is a degraded demo rather than a broken one, and
all three are stated in the README, on the demo page at `/`, and in `/health`.

| Mechanism | Effect |
|---|---|
| Pre-seeded cache | Profiles in `seeds.txt` answer with no live session |
| `X-LI-AT` header | A caller can supply their own session for one request |
| Run it locally | Cookies never leave the reviewer's machine |

`X-LI-AT` is implemented under AGENTS.md §5's three conditions — never logged,
never cached under, never persisted — each with a test. The caching one is the
substantive one: a caller's session may see profiles ours cannot, so caching
their response would serve one caller's private view to everyone afterwards.
Such a request bypasses the cache in **both** directions.

The README does not present the header as the preferred route. It transits a
server the reviewer does not control, and running locally avoids that entirely,
so local is recommended for anything beyond a quick look.

### A test-suite bug this surfaced

The first version of the caller-session tests stubbed the shared client but not
`_client_for`, which builds a throwaway client for a supplied session. Those
tests made **real requests to LinkedIn with a fake cookie** and spent two
minutes retrying. The suite passed; it was simply slow.

AGENTS.md §8 requires the suite to run with no network, and that had been true
by convention rather than by construction. `tests/conftest.py` now blocks
`curl_cffi` at the boundary and fails any test that reaches for it. Runtime went
from 128 s to 0.8 s — the gap was entirely real network traffic nobody intended.
