# Methodology

How this API's data source was found, and what had to be reverse-engineered to
use it. Written against evidence in `docs/evidence/`; every claim below is
either backed by a saved artifact or explicitly marked as unverified.

The short version: **the endpoint the plan was built around no longer exists.**
LinkedIn has migrated the profile surface to a Server-Driven UI that speaks
React Server Components over the wire. Getting structured data back out meant
reverse-engineering that protocol.

---

## 1. The legacy endpoint is gone (HTTP 410)

The intended primary source was:

```
GET /voyager/api/identity/profiles/{public_id}/profileView
```

It returns **410 Gone**:

```json
{"data":{"status":410},"included":[]}
```

410 is not a block. 403 or 999 would be a block. 410 is the explicit HTTP
semantic for *this resource has been permanently removed on purpose*, and it is
what the server chose to send to a request carrying a valid session.

**Scope of that claim, and why no citation is possible.** It was reproduced
twice, 37 bytes both times, and in the same run with the same cookie the Dash
endpoint and the SDUI screen both returned 200, so the session was good and the
410 was specific to this resource.

There is no external source to cite. LinkedIn does not publish Voyager
documentation, so there is no changelog, no deprecation notice and no status
page entry for any of it. Searching turns up the older Python wrappers, which
still document `profileView` as the endpoint to use, and none of them note that
it stopped working. The absence of a citation here is a property of an
undocumented private API, not a gap in the investigation.

So the evidence is: our own reproduction, twice; the browser never calling it
across eight captures; and two other independent implementations of this same
challenge reporting the same 410. That is corroboration rather than proof, on
one profile and one account.

**And it does not mean Voyager is gone.** `identity/dash/profiles` answers 200
today, which is what most other implementations build on. Exactly one resource
under `/voyager/` was found dead. Conflating the two would be the same error as
§20, in the other direction.

Two independent signals agree:

| Check | Result |
|---|---|
| Direct request with a valid session | **410 Gone** |
| Calls to that endpoint in a 140-entry browser HAR | **0** |

The browser never asks for it either. It is not deprecated-but-alive; it is
retired.

### The blast radius is narrower than "Voyager is dead"

Worth stating precisely, because the imprecise version is wrong. The same
capture contains **13 live `/voyager/` calls**, all HTTP 200:

```
/voyager/api/graphql?queryId=voyagerMessagingDashAffiliatedMailboxes...
/voyager/api/graphql?queryId=voyagerSearchDashTypeahead...
/voyager/api/voyagerRelationshipsDashCohorts?decorationId=...
```

Messaging, search typeahead and network recommendations still run on Voyager.
**The migration is per-surface.** The profile surface has moved; the rest of the
platform has not.

> **Correction (see §20).** Even that is too strong. A Voyager endpoint —
> `identity/dash/profiles` — still serves the profile *identity record*. What
> migrated to SDUI is the **section content**: experience, education, skills and
> the rest. The original wording claimed more than the evidence supported.

Evidence: `docs/evidence/rsc-components.md`, `scripts/check_profileview_410.py`.

---

## 2. TLS fingerprinting — measured, and the claim does not hold

The project's stated ground truth is that plain `requests` / `httpx` present a
JA3 TLS fingerprint no real browser produces, and that LinkedIn answers with
**HTTP 999** regardless of a valid session cookie, while `curl_cffi` with Chrome
impersonation returns 200 on the identical cookie.

Everything built here assumes that and uses `curl_cffi`. **The paired
999-vs-200 capture is not yet in `docs/evidence/`.**

There is a specific reason it cannot simply be re-run. `scripts/check_tls_fingerprint.py`
targets `profileView` — the endpoint that now returns 410 to *everyone*. Running
it today yields 410 for both clients and demonstrates nothing about TLS, because
the request never gets far enough to be judged on its fingerprint.

To produce the evidence, the comparison must be retargeted at an endpoint that
is actually alive: `GET /flagship-web/in/{public_id}` with
`x-li-rsc-stream: true`, issued twice with identical cookies and headers, once
through `requests` and once through `curl_cffi`. That is two requests, and it is
worth spending them — this is a graded artifact.

**Now measured — see `docs/evidence/tls-fingerprint.md`.** The result
contradicts the claim. In two experiments, authenticated against Voyager Dash
and unauthenticated against the public profile page, plain `requests` and
`curl_cffi` with Chrome impersonation received **byte-identical** responses:
200/200 in the first, 999/999 in the second. What separated success from 999 was
the presence of a valid session, not the TLS handshake.

A genuine 999 was reproduced, so the status code is real. But on the endpoints
and from the vantage point tested, `curl_cffi` was not load-bearing. It is
retained as cheap insurance rather than as the mechanism, and this document no
longer claims otherwise.

---

## 3. What replaced it: Server-Driven UI over RSC

Profile content is now delivered by two call shapes.

**The screen shell** — one GET, returns the top card and the page structure:

```
GET /flagship-web/in/{public_id}
    x-li-rsc-stream: true
    sec-fetch-mode: cors
```

**Section cards** — one POST each, lazily fetched as the user scrolls:

```
POST /flagship-web/rsc-action/actions/component
    ?componentId=com.linkedin.sdui.generated.profile.dsl.impl.<name>
    &sduiid=<same value>
```

Neither returns JSON. Both return `application/octet-stream` containing a
**React Server Components flight stream**.

### An accident of capture nearly sent this the wrong way

A second HAR, taken while loading the same profile, contained **no RSC at all** —
the profile arrived as 169 KB of server-rendered HTML with every section in it,
including skills and About.

That looked like a better data source, and it is easier to parse. It was
rejected, for two reasons.

The first is that it isn't the assignment. The brief asks for a purely
reverse-engineered solution that hits LinkedIn's endpoints; parsing the rendered
page is the thing one does *instead* of that. Technically it satisfies "no
browser" — it is still an HTTP client hitting a URL — but a reviewer looking for
protocol reverse-engineering would correctly read HTML parsing as scraping.

The second is more interesting. The two captures differ not by device or account
but by **navigation type**:

| Request | Response |
|---|---|
| `sec-fetch-mode: navigate`, no `x-li-rsc-stream` | server-rendered HTML |
| `sec-fetch-mode: cors` **+ `x-li-rsc-stream: true`** | RSC flight stream |

The format is selected by a request header. The HTML was not a discovery about
what LinkedIn serves us — it was an artifact of how the capture was taken. We
choose the protocol; the RSC path is available on demand.

---

## 4. The RSC flight format

The wire format is newline-delimited records:

```
<hex_id>:<payload>
```

Three record kinds appear:

```
3:I["e9e5744c902fddb98f0eb62aee5d400a",[],"TracedComponent"]   module import
2:null                                                          plain value
0:["$","div",null,{...}]                                        React element
```

An element is `["$", tag, key, props]`, where `tag` is either an HTML tag name
or a reference to an imported component.

Three properties make this harder than it looks.

**Records arrive out of order.** They are emitted in completion order, and a
record may reference an id that appears later in the stream. Resolution must be
two-pass: index everything, then walk from the root.

**Ids are hexadecimal.** `1a` is a distinct record, not decimal 26. A parser
that coerces ids to `int` silently mis-resolves roughly one reference in ten.

**Document order is the only ordering signal.** AGENTS.md §2.5 warns that
filtering a flat collection by type returns elements out of order; the same
hazard applies here in a new costume. Reading records by ascending id produces
plausible, wrongly-ordered output — education interleaved into experience. The
only correct traversal is from the root, resolving references as they are met.

### Two reference forms, and a silent failure mode

This is the single most useful finding in this document.

References appear in two forms:

```
"$L35"    lazy module reference
"$35"     direct record reference
```

A small component response uses almost exclusively `$L`. **The full screen
response is dominated by the direct form** — roughly 1,600 occurrences against
1,085 lazy ones.

A parser handling only `$L` against that response:

* indexes all 336 records
* reports **zero errors**
* extracts **nothing**

That is the trap. In a self-describing format, an unhandled reference form does
not surface as a parse failure. Every record decodes as valid JSON, no exception
is raised, and the function returns an empty document. The bug looks like "this
profile has no data" rather than "the parser is incomplete" — and a smoke test
asserting `status == 200` passes clean.

The parser now resolves both forms, and disambiguates on lowercase hex so that
`$undefined` and `$Sreact.fragment` cannot be mistaken for references.

### Extracting text: structure, not naming

The first approach was an allowlist of content-bearing props, derived by
scanning a capture for every prop that transitively held a reference:
`children`, `initialContent`, `buttonProps`, `textProps`.

It did not survive the full screen response, which also hangs subtrees off
`navItemProps`, `renderedChildScreen`, `renderedContentBefore`,
`renderedToolbar`, `resultsContainer` and others. The set differs per response,
and a missed name costs a silent partial parse.

The allowlist was replaced with a structural invariant:

> **Rendered text is always an element of a list.
> Metadata is always a scalar dict value.**

```json
{"children": ["Amazon"]}        → text
{"className": "_02484ad3"}      → not text
```

This holds across both response shapes and does not need updating when LinkedIn
renames a prop. Class names, tracking ids and accessibility labels are excluded
by where they sit, not by being individually named.

---

## 5. Enumerating the component surface from one request

The section cards are lazily fetched, so a HAR only shows the ones that happened
to scroll into view. The first capture showed `profileCardsBelowActivityPart1`
and nothing else, which invited the conclusion that no further parts existed.

That conclusion was drawn, and it was **wrong**. It is left in
`docs/evidence/rsc-components.md` rather than quietly corrected, because the way
it was wrong generalises: absence of evidence in a traffic capture is not
evidence of absence in the API.

The screen shell **declares the components it will lazily fetch**. One GET, and
grep:

```
profileCardsAboveActivity          profileCardsBelowActivityPart2 … Part7
profileCardsActivity               profileCardsBelowActivityPart1WithoutExp
profileCardsExperienceOnly
```

Six further parts and three variants, none of which had appeared in the capture.

Each was then probed once to learn what it renders:

| Part | Sections |
|---|---|
| 1 | experience, education, licenses & certifications |
| 2 | recommendations |
| 3 | courses, honors, patents, publications, test scores |
| 4 | languages, organizations |
| 5 | interests |
| 6 | volunteer causes |
| 7 | **skills** |

This replaces the recon step the plan assumed was necessary — scrolling a
profile fully and re-exporting a HAR. The surface is enumerable from one
request, on any profile.

### An empty section still identifies itself

Parts 2 and 6 render nothing for the test profile, yet both still declare what
they are:

```
observabilityIdentifier: ...profile.components.recommendationsTopLevelSection
initialContent:          "$undefined"
```

Two consequences. The map is discoverable from a sparse profile, so no
"complete" profile is needed to build it. And more importantly, **an empty
section is distinguishable from a failed fetch** — a 200 with
`initialContent: "$undefined"` genuinely has no data and belongs in the schema
as an empty array, while a failed component belongs in `partial_fields`.
AGENTS.md §12 forbids fabricating data to fill gaps; this is the signal that
tells the two cases apart.

---

## 6. What the endpoints do and do not validate

Tested by sending **freshly generated** values rather than replaying captured
ones:

| Header | Validated? |
|---|---|
| `x-li-pageforestid` | No |
| `x-li-page-instance` | No |
| `x-li-page-instance-tracking-id` | No |
| `x-li-traceparent` / `tracestate` | No |
| `parentSpanId` (query param) | Not required at all |
| `miniProfileUrn` (query param) | **Not required** |

All four telemetry headers were minted at request time and the call returned
200. They are client-side telemetry, not server-issued tokens.

The give-away was visible in the capture before it was tested:
`x-li-pageforestid` is byte-identical to the trace id inside `x-li-traceparent`,
and W3C traceparent ids are generated by the client:

```
x-li-pageforestid : 00065a2be64edbf4009d0b718c5aeb93
x-li-traceparent  : 00-00065a2be64edbf4009d0b718c5aeb93-0c40f75a6cc2f079-00
```

**Consequences for the client:**

* No warm-up request to `/in/{public_id}/` is needed. The endpoint can be called
  cold.
* `miniProfileUrn` being optional removes a bootstrap step: a profile URL
  resolves in **one** request, not a slug-to-id lookup followed by a fetch. The
  durable `ACoAA…` id is recoverable from the shell response for the component
  POSTs that do need it.

### The POST body is reconstructible

The component request carries a 3 KB JSON body containing
`profileComponentState` — thirteen `BindingImpl` entries that look opaque. They
are not. Every key follows one template:

```
ProfileComponentState{Name}{vanityName}ProfileComponentState
```

The body is rebuilt from the vanity slug alone. Only **4 of the 13** state keys
were included and the requests still returned 200, so the endpoint tolerates
substantial omission. Nothing needs to be copied from a live browser session.

---

## 7. The data is pre-rendered display text

This is the real cost of the migration, and it is not recoverable by better
parsing.

The old typed API returned `{"startDate": {"month": 8, "year": 2022}}`. The SDUI
layer returns the string the server already formatted for a human:

```
"Full-time · 4 yrs 1 mo"
"Aug 2022 - Jul 2025 · 3 yrs"
"Hyderabad, Telangana, India · On-site"
```

Employment type, duration, work mode and date ranges must be recovered by regex
from prose. That is lossier and locale-dependent, and the schema says so
per-field via `_meta.parse_confidence`.

### The separator characters are not what the plan assumed

Verified byte-by-byte, because guessing here fails silently:

| String | Separator | Codepoint |
|---|---|---|
| `Aug 2022 - Jul 2025` | hyphen-minus | **U+002D** (experience) |
| `2018 – 2022` | en dash | **U+2013** (education) |
| `Full-time · 4 yrs 1 mo` | middle dot | U+00B7 |

**Experience and education do not use the same dash.** Matching only the en dash
— as originally specified — parses education correctly and silently drops every
experience date. The schema stays populated and plausible, so no smoke test
catches it.

A degree string also carries its own hyphen
(`"Bachelor of Engineering - BE, Electrical..."`), so a range split must be
anchored on surrounding whitespace rather than the bare character.

### Truncation is advertised

Cards render a capped number of entries and state the true total in a "see all"
control:

| Control | Shown | Actual |
|---|---|---|
| `Show all 6 licenses` | 2 | 6 |
| `Show all 4 languages` | 2 | 4 |
| `Show all` | 4 | **unstated** |

AGENTS.md §2.7 requires detecting truncation rather than silently returning a
partial list. Where LinkedIn states a total it is recorded exactly; where the
control reads only "Show all", `total` stays `null` and the warning says
"an unstated number" rather than guessing.

---

## 8. Segmenting entries

Entries are grouped by the entity URL every string in an entry shares
(`/company/1586/`). Within a group, roles are delimited by **date-range
strings**, not by a fixed number of lines.

The original plan proposed treating each run of three strings as one entry. Real
data breaks it immediately, because LinkedIn renders two shapes:

```
Amazon                                   grouped: employer header
Full-time · 4 yrs 1 mo                   type + total, hoisted
Hyderabad, Telangana, India · On-site    location, hoisted
SDE 2  /  Aug 2025 - Present             role 1
SDE    /  Aug 2022 - Jul 2025            role 2
```

```
ML intern                                single: title first
Ravenn · Internship                      company · type
Oct 2020 - Dec 2020 · 3 mos              dates
```

Counting date ranges separates them: two or more means a grouped entry whose
opening lines are employer-level and inherited by every role; exactly one means
a single role.

**Known ambiguity.** A grouped entry containing exactly one role would present a
single date range and be read as the single shape, taking the company name as
the job title. No such entry appears in the captured data — LinkedIn appears to
render a lone role in the single shape anyway, which would make the case
unreachable — but that is an inference from one profile, not a verified rule.

**Confidence.** Validated against **two** profiles. The second one broke the
first version of this code in four separate ways, each of which is now a
regression test:

* **Inline skill annotations.** `"Skills for Example Lead at EXAMPLE-ORG"`
  renders *between* roles inside the experience card and carries no entity URL.
  It therefore split one employer into several groups, stranding every role
  after the first — four entries silently dropped, and two whose "company" came
  out as `"Full-time"` and `"89826258"`.
* **Per-role locations.** A grouped entry may give each role its own location
  rather than hoisting one to the employer.
* **Single-word locations.** `"Bangalore"` has no comma, so a punctuation-based
  test discarded it. The line after a role's dates is now identified
  structurally: it is a location precisely when it is not the line before the
  next role's dates.
* **Four-line certifications** with a credential id, plus a "Show credential"
  control in a *different* section carrying the credential URL wrapped in a
  `/safety/go/` redirect.

The lesson generalises beyond this parser: **the first profile validated the
happy path, and the second found the bugs.** A third would likely find more. The
`_meta.warnings` channel exists so that these failures are visible rather than
silent — the four dropped entries announced themselves as
`"no recognisable date range"` rather than simply not appearing.

---

## 9. Request cost

A full profile is **1 + 7 = 8 requests**: one shell, seven components. The
default fetches five sections in **6** requests.

Part 5 (interests) is ~196 KB, roughly 70% of the total payload, and maps to no
field in the public schema. It is excluded by default, which makes the `fields`
parameter a genuine cost control rather than decoration.

Every outbound request made during this work is recorded in
`docs/evidence/request-log.jsonl` — method, URL, redacted headers, status,
response size and classification. Credentials appear only as truncated SHA-256
fingerprints, enough to correlate two requests to one session and useless
otherwise. The log is generated by the same module the running API uses, so the
audit trail continues in production rather than stopping at the research phase.

Total requests made against LinkedIn while developing this: **7**.

---

## 10. Limitations

* **The TLS 999-vs-200 evidence is outstanding** (§2), and the existing script
  points at a dead endpoint.
* **Everything is validated against one profile.** The segmentation heuristics,
  the section labels and the component map all come from a single subject.
* **`about` is not yet mapped.** It lives in `profileCardsAboveActivity`, which
  has not been probed.
* **Company identifiers are numeric** (`1586`, not `amazon`). The RSC layer
  exposes only numeric ids; the vanity slugs appear in the HTML surface that was
  deliberately not used.
* **Locale.** The parsers assume English month abbreviations and English type
  labels. A profile whose `defaultLocale` differs will yield strings they do not
  match; each degrades to a null field with a warning rather than raising.
* **This is a moving target.** Component ids are generated from LinkedIn's build
  (`com.linkedin.sdui.generated...`) and the client version is pinned at
  `0.2.7003`. Both will drift. The structural extraction rule is designed to
  survive prop renames, but not a protocol change.


---

## 11. Cross-checking against LinkedIn's own PDF export

LinkedIn's "Save as PDF" is produced by a **different code path** from the API,
which makes it an independent oracle: it can confirm the parse without trusting
the parser.

Diffing a PDF export against the API output for the same profile:

| Section | Result |
|---|---|
| Experience | **Exact match** — 5/5 entries: titles, employers, date ranges, per-role locations |
| Education | Exact match, including degree and field split |
| Skills | **Found a real bug** (below) |
| Certifications | Three different counts (below) |

### The bug it found

The API was reporting six skills. The PDF listed three. The extra entries were
the person's **certifications**, which the skills card renders beneath each
skill as supporting evidence:

```
Wireshark                                  bold    <- the skill
Certified Network Security Practitioner    normal  <- evidence for it
Cyber                                      bold    <- the skill
Cybersecurity Fundamentals                 normal  <- evidence
Information Technology Fundamentals        normal  <- evidence
Google Cybersecurity Specialization        normal  <- evidence
```

They are siblings in one text run, with no structural nesting to separate them.
Treating each line as a skill turned certifications into fabricated skills —
plausible-looking wrong output of exactly the kind AGENTS.md §12 forbids, and
invisible without an external reference.

`textProps.fontWeight` is the only available discriminator. The parser now
surfaces it as `TextNode.emphasis` and the mapper takes bold entries as skills,
attaching the rest as `credentials`. Where a card declares no weight — the
endorsement layout does not — it falls back to the previous behaviour.

This is the one place where presentation styling is load-bearing. That is
uncomfortable, and it is recorded as such: it will break if LinkedIn restyles
the card. The alternative was to keep emitting fabricated skills.

### Three different answers for one count

| Source | Certifications |
|---|---|
| API response (card as rendered) | 2 |
| LinkedIn's own "Show all N" control | **7** |
| LinkedIn's own PDF export | **5** |

LinkedIn disagrees with itself. The card renders two, its own control claims
seven, and its own PDF exports five. This is worth stating plainly for anyone
who assumes a single authoritative count exists: there is not one. The API
reports what it received (`returned: 2`) alongside what LinkedIn claimed
(`total: 7`), and does not attempt to reconcile them.

### Why this belongs in the methodology

Two profiles were enough to find the structural bugs. Neither was enough to find
this one, because the output was *internally consistent* — six well-formed
skills, no warnings, all tests green. It took an independent rendering of the
same underlying data to show that four of them were not skills at all.

The general lesson: for a scraper, tests written against your own parse only
prove the parser is self-consistent. An oracle produced by a different code
path is what proves it is correct.


---

## 12. A third capture: About, and two more parser bugs

A 127 MB HAR of a browsing session (519 entries, 427 with bodies) added the one
component the earlier captures never triggered, and broke the parser twice more.

### About lives in `profileCardsAboveActivity`

Not a numbered part. The client now addresses components by name rather than by
part number, and `about` is fetched by default.

The test profile's About is **not empty — it is padded with U+3164 HANGUL
FILLER**, five invisible characters that render as blank. `str.strip()` does not
remove them, because they are not Unicode whitespace. Reported naively, the API
would have returned a five-character "biography" of invisible padding.

`visible()` now strips zero-width and filler characters and returns `None` when
nothing remains. LinkedIn's own PDF export shows the same thing: a "Summary"
heading followed by nothing.

### Serialised JSON in a text slot

The skills detail screen carries a JSON blob in a value position:

```json
{"threadlineDecoration":null,"key":"3c23b186-...","semanticId":""}
```

It is a string inside a list, so the structural extraction rule (§4) admitted it
as rendered text — and it would have surfaced as a profile field. Strings that
parse as a JSON object or array are now skipped.

This is the cost of the structural rule: it is robust against prop renames, but
it cannot tell a serialised payload from prose without checking. The check is
cheap and the failure it prevents is loud.

### Detail screens return untruncated lists

The capture revealed endpoints behind LinkedIn's own "Show all" controls:

| Endpoint | Returns |
|---|---|
| `skills` | the full skills list, each with what evidences it |
| `certifications` | the full certification list |
| `profileCardsExperienceOnly` | experience in isolation |

The card endpoints cap entries; these do not. Wiring them up would close the
truncation gap that `_meta.coverage` currently only reports. Not yet
implemented, and recorded here as a known, addressable limitation rather than an
inherent one.


---

## 13. The screen response is not deterministic

Fetching the same profile twice, seconds apart, with an identical request:

| Attempt | HTTP | Top card |
|---|---|---|
| 1 | 200 | **absent** — no name, headline, location or follower count |
| 2 | 200 | present — "Raj Shamani", 1,804,185 followers |

Nothing in the response distinguishes the two. Both are 200, both parse cleanly,
neither reports an error. The first simply does not contain the top-card
components.

This is worth stating because it defeats the obvious testing strategy. A single
successful fetch does not establish that a field is reliably available, and a
single failed one does not establish that it is missing. Anything built on this
data source has to treat section presence as probabilistic.

The API previously handled this badly: with no top card it fell back to the URL
slug as the member's name and returned 200 with no warning, so
`"name": {"full": "rajshamani"}` looked like a real answer. That is precisely
the confident-but-wrong output AGENTS.md §12 forbids.

It now adds `top_card` to `_meta.partial_fields` with an explicit warning
naming the retry. The slug is still returned so the response stays
schema-valid — but a consumer can tell it apart from a real name.

There is a reasonable argument that this should be a 206 rather than a 200.
It is left at 200 because the rest of the profile is complete and correct; the
`_meta` block is where the distinction belongs.


---

## 14. Two more captures: About truncation, and honours

Captures of two further profiles — one with a populated About, one with honours
and awards — added a section and exposed a silent truncation.

### About is one node per paragraph

The earlier test profiles had an empty About, so a single-node assumption went
unchallenged. A profile with a real summary renders it as **four separate
nodes**, one per paragraph:

```
Hello connections,
I'm a passionate researcher with a background in Electronics and ...
My journey has equipped me with strong skills in literature surveys ...
What drives me is the desire to solve root problems ...
```

Taking the first would have returned `"Hello connections,"` as the entire
biography — a well-formed, confident, almost-empty answer. Paragraphs are now
joined.

This is the second time an empty section has hidden a bug in how the populated
version is handled. Testing against profiles that *lack* a section proves only
that the absence is handled.

### Honours, and the right entry boundary

Honours (`profile-card-honors-and-awards`, in `Part3`) render as a title plus up
to three optional qualifiers:

```
Example Student Award          <- title
Issued by EXAMPLE-PUBLISHER · Dec 2024            <- issuer + date
Associated with EXAMPLE-COLLEGE  <- association
Received the prestigious Example Student Award...   <- description
```

Two qualifiers announce themselves with a prefix. The description does not — and
**a description is indistinguishable by content from the next entry's title.**
A content heuristic here swallows the following award.

Neither of the discriminators used elsewhere helps: `fontWeight` is `None` on
the title and `normal` on the description, the reverse of the skills card.

The correct signal is `componentKey`: every line of one entry shares one, and
the media button beneath carries a different one. It is an exact delimiter
rather than an inference, and it is what the mapper now uses.

Worth noting for anyone extending this: **the right entry boundary differs per
card.** Experience groups by navigation URL, skills split on font weight,
honours split on componentKey. There is no single rule, and assuming one
produces output that looks right on whichever card you tested.


---

## 15. A fifth profile: no new bugs

A capture of a profile with two degrees, five positions and a credential-bearing
certification introduced several shapes none of the earlier profiles had:

* **two education entries** (every prior profile had exactly one)
* **an ongoing degree** — `"Aug 2021 – Present"`, an en-dash range with an open
  end, where "Present" had only ever been seen with the ASCII hyphen used by
  experience
* **an education entry with no dates at all**
* a `Skills for ...` annotation inside the *certifications* card, a section that
  had only ever carried them in experience

All parsed correctly with no changes. That is the first capture to find nothing,
and it is worth recording as evidence rather than as an absence of news: the
grouping rules — entity URL for education, componentKey for honours — held on
shapes they were not written against.

It is not proof of generality, and the two remaining unobserved sections were
resolved separately — see below.


---

## 16. Recommendations: the card does not contain the recommendations

The last two unobserved sections were finally seen populated, on a profile
viewed from a connected account.

**Volunteer causes** (`Part6`) is trivial — one dot-separated line,
`"Education • Science and Technology"` — and is now mapped.

**Recommendations** (`Part2`) is more interesting. Empty, the component returns
1,122 bytes. Populated, it returns **60,509**. That looked like a substantial
payload to parse.

My first reading of that payload was that it contained only three strings —
`Recommendations`, `Received (2)`, `Given (2)` — and that the recommendation
text must be fetched separately when a tab is clicked.

**That was wrong, and the way it was wrong is the most instructive bug in this
document.** The text was in the response the whole time. The parser could not
see it.

So the honest outcome is a partial one. The API returns the counts, which are
real data:

```json
"recommendations": { "received_count": 2, "given_count": 2 }
```

and does not return the text, because obtaining it needs an endpoint we have not
observed. Capturing it would require a HAR taken while clicking the tab.

Two points worth drawing out:

* **Response size is not a proxy for data.** A 60 KB response yielding three
  strings, next to a 1.1 KB response yielding a full honour entry, is a useful
  corrective to sizing work by payload weight.
* **Absent counts are `null`, not `0`.** Zero asserts the profile has no
  recommendations; null says we did not determine it. On a profile where the
  card is not fetched, those are very different claims.


---

## 17. A response is not always a single tree

Searching the recommendations payload as **raw text** rather than through the
parser immediately turned up what the parser had missed:

```
"An example recommendation body..."
```

The parser reported three text nodes. The raw response contained full
recommendation bodies. The discrepancy was not a walking bug — the record
holding that text is perfectly well-formed and reachable — it was an assumption
made in the very first version of the parser and never revisited:

> the document root is record `0`

That holds for every component captured until now. It does not hold here.
Computing which records are referenced by another shows **three** unreferenced
records: `0`, `14` and `d`. Record `0` carries the tab headers. Record `d`
carries 38 nodes of recommendation bodies. Nothing links them; they are siblings
in one response, and RSC does not require a single tree.

The parser now walks every unreferenced record, `0` first.

### Why this went unnoticed for so long

The failure was invisible from inside. Every record decoded, no error was
raised, the reachable subtree parsed correctly, and the output — a card title
and two tab labels — was entirely plausible for a card whose content loads
lazily. I then reasoned from that plausible output to a confident and wrong
architectural conclusion, and wrote it up.

What broke it was reading the raw bytes instead of the parsed result. That is
the same lesson as the PDF cross-check in §11, arriving by a different route:
**a parser cannot be validated against its own output.** Both times, the error
was found by consulting something outside the parser — LinkedIn's PDF export in
one case, the undecoded response body in the other.

### What the entries actually contain

Once reachable, each entry parses cleanly:

```
Alex Doe    3rd+  2025-02  Chief Technologist
   3 paragraphs: "Example recommendation text..."
```

Two further details were needed:

* **Author names have no marker.** What identifies one is that the *next* node
  is a connection distance (`· 3rd+`). Entries are delimited by a one-node
  lookahead; tracking state forwards absorbs each author into the previous
  entry's body.
* **There is a third tab, `Pending`,** which repeats the Received entries
  verbatim. It is not mapped, but it *must* be recognised as a boundary — left
  unhandled, its entries continue into whichever list was open and are counted
  twice.

Parsed entry counts now match the counts LinkedIn declares in its own tab
headers, which is a useful self-check: `received=2, given=2` against
`Received (2)`, `Given (2)`.


---

## 18. Solving truncation: detail screens are not where the data is

The cards cap how many entries they render. Closing that gap took three steps,
and the first two were dead ends worth recording.

**The detail screen is chrome.** LinkedIn's "Show all" control navigates to
`POST /flagship-web/in/{vanity}/details/skills/`, whose body is a
`NavigateToScreen` action reconstructible from the vanity name alone. It returns
200 and parses cleanly — and contains the page title, four filter tabs
("All", "Industry Knowledge", …) and a recommendations widget. **No skills.**

**The entries come from a pager.** The list is fetched separately from
`com.linkedin.sdui.pagers.profile.details.skills`, and paging uses `start` and
`count` — the same parameter names the legacy Voyager API used (AGENTS.md §2.7).
That convention survived the SDUI migration intact.

**The pager is a different endpoint.** Posting the pager body to
`/rsc-action/actions/component` — the endpoint every other component uses —
returns **500**. Pagers live at `/rsc-action/actions/pagination`, keyed by
`sduiid` rather than `componentId`. The referer must also point at the detail
page rather than the profile.

### Result

| Section | Card | Paged |
|---|---|---|
| skills | 2 | **26** |
| certifications | 2 | **7** |

The certification count now matches the total LinkedIn states in its own "Show
all 7 licenses" control — an independent check that the paging terminated in the
right place.

### Paged responses carry chrome the cards do not

Three things appear in a paged feed and in no card, each of which parsed as
content on the first attempt:

* `"Skills:"` followed by the skills a certification evidences — read as an
  authority, and its skill list as the next certification's name.
* A build version string, `"0.1.51189"`, trailing the final page.
* Empty-state copy (`"Nothing to see for now"`) on the page past the end, which
  is also what now terminates paging.

That produced **13** certifications where LinkedIn says 7 — inflated, not
truncated, which is the more dangerous direction: a caller checking
`returned >= total` would have concluded the list was complete.

### Two truncation signals that look alike

`"Show all 6 licenses"` means the *list* is truncated. `"Show all 4 details"`
expands one entry's supporting credentials and says nothing about the list.
Treating them alike marks a fully-paged section incomplete forever.

Related: the card renders its "see all" control whether or not the list was
subsequently paged, so the truncation flag is now cleared once the entries held
match the total LinkedIn declares.


---

## 19. What the coverage survey found

Six profiles, fetched live through the same client and mapper the API uses.
The survey exists to produce `COVERAGE.md`, but its first run was more useful
as a bug-finder.

### A single-token location was being dropped

`location` came back at 67%. The two misses were not a fetch failure — the data
was in the response both times. The mapper identified the location as *the first
line after the headline containing a comma*, which works for
`"Seattle, Washington, United States"` and fails for `"United States"`.

This is the same failure as the single-word `"Bangalore"` in experience (§14),
in a different function: **inferring structure from punctuation inside a value.**
Fixed there structurally, and missed here.

The top card has a reliable anchor — a standalone middle dot separating the
location from the connection count:

```
Reid Hoffman
Co-Founder, LinkedIn, Manas AI & Inflection AI...
He/Him                       <- optional
United States                <- the line before the dot
·
Contact info
```

Anchoring on the dot took `location` from 67% to **100%**. Pronouns fell out of
the same rewrite, since they occupy a known slot.

### Failed requests were not reaching the audit log

One profile failed mid-survey with `UpstreamBlocked` after 0.2 s — too fast for
a round trip. The request log showed nothing: transport-level failures raised
*before* the logging call, so the only requests recorded were ones that got a
response.

An audit log that records successes and drops failures is worse than none: it
reads as a complete account while omitting exactly the events worth auditing.
Attempts are now logged with `status: 0` and the transport error.

The failure was transient and the same profile succeeded on retry, which
prompted the second fix: `MAX_ATTEMPTS` with exponential backoff and jitter, as
AGENTS.md §7 asked for. Jitter matters because concurrent failures otherwise
retry in lockstep and reproduce the burst that caused them.

### Numbers the survey does report

| | |
|---|---|
| Fetched successfully | 6/6 |
| p50 / p95 latency | 5.3 s / 12.6 s |
| `location`, `name`, `headline`, `experience` | 100% |
| `about`, `follower_count` | 67% |

The remaining 67% figures are **not** parser gaps. Both misses are the same two
profiles, and both were verified by hand: one has an About padded with invisible
filler (§12) and neither displays a follower count. The report shows scalars per
profile rather than only as a rate precisely because an aggregate cannot
distinguish "we failed to parse it" from "the member does not have one".

`courses` and `volunteer_causes` sit at 0% for this sample. They parse correctly
against captures that do contain them, so the figure describes the sample, not
the mapper — and they remain the least-exercised paths.


---

## 20. A correction, and which approach is actually better

Three other public implementations of this brief were found after the fact. All
three build on an endpoint this project never tested:

```
GET /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}
```

It appears in **none** of the eight browser captures taken here. The flagship web
client does not call it, and the method used throughout this project was traffic
capture — so the method could not have found it. That is a real limitation of
capture-driven reverse engineering, and it is worth naming rather than
explaining away: *what the client no longer calls is invisible, even when it
still works.*

### Measured, one session, one target, four variants

| | Endpoint | Client | Result |
|---|---|---|---|
| A | `identity/dash/profiles` | plain `requests` | **200**, 13,448 B |
| B | `identity/dash/profiles` | `curl_cffi` chrome142 | **200**, 13,448 B — byte-identical |
| C | `identity/profiles/{id}/profileView` | `curl_cffi` chrome142 | **410 Gone** |
| D | SDUI screen (this project) | `curl_cffi` chrome142 | **200**, 731,133 B |

Reproduced with a fresh session. Two findings follow, and one of them is
inconvenient.

**TLS impersonation is not load-bearing for `dash/profiles`.** A plain client
gets a byte-identical response. The other implementations use ordinary HTTP
clients, and on that endpoint they are right to. The blanket claim that a
non-browser TLS fingerprint is answered with HTTP 999 does not hold here.

**`dash/profiles` is not a substitute for the SDUI path.** It returns the
identity record only. Experience and education appear as
`fsd_profileCard` URN *pointers*, not as data:

```
experienceCardUrn = urn:li:fsd_profileCard:(ACoAA…,EXPERIENCE,en_US)
educationCardUrn  = urn:li:fsd_profileCard:(ACoAA…,EDUCATION,en_US)
```

### So which is better? Neither — and that is the useful answer

They are better at different things, and the honest comparison is field by
field.

| | Voyager Dash | SDUI / RSC |
|---|---|---|
| Experience, education, skills, certifications | ✗ pointers only | ✓ the only source |
| Typed values | ✓ | ✗ pre-rendered display strings |
| Locale variants (`multiLocale*`) | ✓ | ✗ absent entirely |
| Image URLs with expiry | ✓ | ✗ absent entirely |
| Authoritative first/last name | ✓ | ✗ must split on whitespace |
| Industry, websites, premium, verified | ✓ | ✗ |
| Needs TLS impersonation | ✗ | untested — see §2 |
| Cost | 1 request, 13 KB | 1 + N requests, ~730 KB |

The Voyager record is **better data** wherever it has any: typed, locale-aware,
and carrying fields the SDUI payload does not express at all. The SDUI path is
the **only** source for the sections that make a profile a profile.

Using one alone is a mistake in either direction. This project now uses both:
identity from Voyager, sections from SDUI, with Voyager's typed values taking
precedence where the two overlap. `_meta.parse_confidence` marks the name
`raw` when Voyager supplied it and `parsed` when it was inferred by splitting,
so a consumer can tell which they received.

### What adopting it fixed

Three requirements this project was failing, closed by the identity record
rather than by more parsing:

* **Locale variants.** Required by the brief; previously not implemented at all,
  because the SDUI payload does not contain them.
* **Image `expires_at`.** Also required. Signed LinkedIn media URLs expire, and
  the expiry is in the `e=` parameter of the signed path — recoverable only from
  the Voyager record.
* **First/last name.** Previously `full.split()`, which mangles multi-word
  surnames and mononyms. Now taken from LinkedIn, with `split_inferred` marking
  the fallback when it is not.

### What the other implementations get wrong

Not a criticism of their engineering — both deployed demos are honest about
their state — but relevant to the trade:

* Building on `dash/profiles` alone yields identity fields and URN pointers. To
  reach experience or education, those card URNs must be resolved through a
  further endpoint. Any implementation that stops at the identity record is
  returning a profile without the sections.
* One of the two deployed demos reports `linkedInSession: false` at its own
  readiness endpoint; the other documents that serverless egress is refused by
  LinkedIn. Neither published an API key, so neither can be exercised by a
  reviewer arriving from the repository. Those are deployment and access
  decisions rather than data-layer ones — but they are what a reviewer meets
  first.
