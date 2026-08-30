# LinkedIn Profile API

Give it a LinkedIn profile URL, get structured JSON back.

No browser anywhere. No Selenium, no Playwright, no Puppeteer, no headless
Chrome. Just HTTP against LinkedIn's private endpoints.

**Live:** https://scrape-linkedin-boe4.onrender.com

```bash
curl -H "X-API-Key: demo-key" \
  "https://scrape-linkedin-boe4.onrender.com/v1/profile/williamhgates"
```

---

## Try it

> [!IMPORTANT]
> **The first request may take 30 to 60 seconds.** This runs on Render's free
> tier, which sleeps after about fifteen minutes of inactivity. That first call
> is waking the container, not doing any work. Warm it up first and everything
> after is fast:
>
> ```bash
> curl https://scrape-linkedin-boe4.onrender.com/health
> ```

> [!NOTE]
> **Our LinkedIn session has probably expired by the time you read this.**
> A `li_at` cookie lasts weeks, submissions get read later than that, and the
> account we tested with was soft blocked once already for making too many
> requests too quickly.
>
> That is expected, and the demo is built for it. These six profiles are
> pre-cached and return real data with **no live session at all**:
>
> ```
> williamhgates   reidhoffman   rajshamani   cachemoney   cooktim   tmsagarofficial
> ```
>
> Check `/health` to see the session state, and `GET /v1/cache` to see exactly
> what is cached. Any other profile needs a live fetch, and if ours is dead you
> get a clear **503** rather than invented data.

If you want live data on an arbitrary profile, send your own session and bypass
ours entirely:

```bash
curl -H "X-API-Key: demo-key" \
     -H "X-LI-AT: <your li_at cookie>" \
     -H "X-LI-JSESSIONID: <your JSESSIONID cookie>" \
     "https://scrape-linkedin-boe4.onrender.com/v1/profile?url=https://www.linkedin.com/in/someone/"
```

> [!TIP]
> Your cookies are never logged, never cached and never persisted, and there are
> tests for each of those three. They do still transit a server you do not
> control, so if that matters, clone the repo and run it locally instead. It
> takes about a minute.

---

## What comes back

```json
{
  "schema_version": "1.0",
  "profile": {
    "public_id": "williamhgates",
    "profile_url": "https://www.linkedin.com/in/williamhgates/",
    "name": { "first": "Bill", "last": "Gates", "full": "Bill Gates" },
    "headline": "Chair, Gates Foundation and Founder, Breakthrough Energy",
    "location": { "raw": "Seattle, Washington, United States", "country_code": "US" },
    "about": "Chair of the Gates Foundation...",
    "experience": [{
      "title": "Co-chair",
      "company": { "name": "Gates Foundation", "url": "https://www.linkedin.com/company/8736/" },
      "employment_type": "full_time",
      "start": { "year": 2000, "month": 1 },
      "end": null,
      "is_current": true,
      "duration_months": 320
    }],
    "education": [...], "skills": [...], "certifications": [...],
    "languages": [...], "images": { "profile": { "url": "...", "expires_at": "..." } }
  },
  "_meta": {
    "source": "cache",
    "parse_confidence": { "experience[0].start": "parsed" },
    "coverage": { "profile-card-skills": { "returned": 2, "total": null, "truncated": true } },
    "warnings": ["skills: LinkedIn returned 2 of an unstated number of entries"]
  }
}
```

> [!IMPORTANT]
> **Read `_meta` before trusting a field.** Three things there matter:

>
> * `parse_confidence` says `"parsed"` when a value was recovered by regex from
>   a display string like `"Aug 2022 - Jul 2025"`, and `"raw"` when LinkedIn
>   gave it to us typed. Most dates are parsed. That is a real difference in
>   reliability and we would rather show it than hide it.
> * `coverage` separates "this member has none" from "LinkedIn only sent us the
>   first two". Where LinkedIn states a total we record it exactly. Where it
>   does not, `total` stays null instead of us guessing.
> * `source` is `live`, `cache` or `partial`.

---

## API

| Method | Path | |
|---|---|---|
| GET | `/health` | Session state, cache stats, request budget |
| GET | `/v1/profile?url=...` | Fetch by profile URL |
| GET | `/v1/profile/{public_id}` | Fetch by vanity slug |
| GET | `/v1/cache` | What is cached, and which entries were pre-seeded |
| DELETE | `/v1/cache/{public_id}` | Remove a profile from the cache |
| GET | `/docs` | Interactive OpenAPI |

Auth is `X-API-Key`. The demo key is `demo-key`, published deliberately: a
reviewer who hits 401 with no way in just closes the tab. Send no key and the
401 tells you the key and shows you a working curl.

**Parameters:** `refresh=true` bypasses the cache. `fields=experience,skills`
narrows the fetch and cuts requests. `complete=true` pages past LinkedIn's card
limits, which is the difference between 2 skills and 26.

**Status codes:** 200, 206 partial, 400 bad URL, 401 no key, 403 not visible,
404 no such profile, 429 rate limited, 503 upstream blocked with no cached copy.
Never a bare 500 and never a 200 full of nulls.

---

## How it works

```
GET /v1/profile?url=...
   |
   +-- parse URL to public_id            400 if it is not a member profile
   +-- cache hit? ---------------------> return, _meta.source = "cache"
   |
   v  miss
LinkedIn client (curl_cffi, Chrome TLS)
   |
   +-- GET  /voyager/api/identity/dash/profiles     typed identity fields
   |         name split, locale variants, image expiry, industry, websites
   |
   +-- GET  /flagship-web/in/{id}                   top card, durable profile id
   +-- POST /rsc-action/actions/component  x N      experience, education,
   |                                                skills, certs, languages...
   +-- POST /rsc-action/actions/pagination x N      only when ?complete=true
   |
   v
RSC flight parser -> ordered text nodes tagged by section
   |
   v
Mapper -> public schema + _meta
   |
   v
Cache, then respond
```

Two data sources, used where each is better. Voyager returns typed values and
things the other path does not expose at all, but for experience and education
it only returns URN pointers, not content. The RSC path is the only place that
content actually lives. So identity comes from one and sections from the other.

---

## How we got here

The short version, because the long version is in
[METHODOLOGY.md](METHODOLOGY.md).

**One Voyager endpoint is dead. Voyager itself is not.** That distinction
matters, because most of the older writeups and every Python wrapper on GitHub
point at the retired one.

| Endpoint | Result |
|---|---|
| `/voyager/api/identity/profiles/{id}/profileView` | **410 Gone**, 37 bytes |
| `/voyager/api/identity/dash/profiles?q=memberIdentity` | **200**, typed JSON |
| `/flagship-web/in/{id}` and the RSC component actions | **200** |

All three were requested in the same run, with the same cookie and the same
client, so the 410 was specific to that resource rather than us being blocked.
Reproduced twice. The browser never calls the retired one either, zero times
across 140 captured requests, and two other people solving this same problem
independently report the same 410.

**There is no official confirmation of this.** LinkedIn does not document
Voyager at all, so there is no changelog or deprecation notice to point at. The
evidence is empirical: our own reproduction, plus corroboration from other
implementations. One profile, one account. Treat "retired" as the reading the
evidence best supports.

This project uses the live Dash endpoint for identity fields and the RSC layer
for everything else, so nothing here depends on the retired one.

**So we captured traffic instead.** Eight HAR exports of a real logged-in
browser session, loading profiles and scrolling through every section. That
showed the browser never calls the old endpoint even once, and that profile
content now arrives as **React Server Components flight streams**, not JSON.
Parsing that wire format is most of the work in this repo.

**Then we found what the browser does not do.** Other people solving the same
problem were using `/voyager/api/identity/dash/profiles`, which appears in
**zero** of our eight captures. The web client simply stopped calling it, but it
still works. Traffic capture can only ever show you what the client currently
does, and an endpoint can be alive and unused. We wrote that up rather than
quietly folding it in.

**We nearly got the test account restricted.** Partway through, every request
started returning a redirect loop, LinkedIn 302ing us to the same URL forever.
The cause turned out to be our own tooling: a single failed request was
following 30 redirects, and our retry logic tried it three times, so one logical
call became roughly 90 requests to LinkedIn. A short probe script generated a
few hundred requests while believing it was being careful. The session was soft
blocked for hours.

That is why there is a daily budget, a low per minute limit, redirect capping
and no retry on a redirect loop. It also taught us the thing worth knowing:
**burst rate matters far more than daily total.** We made 300+ requests across
several days with no trouble, then tripped a block inside six minutes.

**And that is why the cache is pre-warmed.** A LinkedIn cookie lasts weeks and
a submission gets read later than that, so the demo is seeded with real profiles
that answer with no session at all. It is verified with the credentials removed,
not just assumed to work.

---

## Run it yourself

```bash
git clone https://github.com/tmsagarofficial/scrape-linkedin
cd scrape-linkedin
pip install -e ".[dev]"

cp .env.example .env      # add your own LI_AT and JSESSIONID
uvicorn app.main:app --reload
```

Then open http://localhost:8000/docs.

Get the cookies from DevTools, Application, Cookies, `https://www.linkedin.com`.
LinkedIn's login flow throws a JavaScript challenge that cannot be solved
headlessly, so supplying the cookie directly is the documented answer rather
than a shortcut.

```bash
pytest        # 230 tests, no network, no credentials
```

The suite blocks network access at the boundary, so a test cannot accidentally
call LinkedIn. That is enforced, not just intended: it caught us once.

---

## Deployment

Running on **Render free tier, Singapore region**, which is the closest region
to where the session cookie was issued.

> [!NOTE]
> Free tier means **no persistent disk** and the instance **sleeps when idle**.
> The pre-seeded cache is therefore supplied as a base64 secret file that loads
> at startup, so a cold start still comes up able to answer. The seed is scraped
> profile data and never enters the repository.

Full notes, including Cloud Run and other hosts, are in
[DEPLOYMENT.md](DEPLOYMENT.md). One correction worth repeating here: we
originally assumed a residential proxy was required because LinkedIn blocks
datacenter IPs. We tested that against other live deployments and it is **not
true**. They fetch live from datacenter IPs with no proxy. What actually breaks
deployments is the session expiring.

---

## Known limitations

**Values are reconstructed from display strings.** The old API returned
`{"month": 8, "year": 2022}`. The RSC layer returns `"Aug 2022 - Jul 2025"`, so
employment type, duration, work mode and dates come back through regex.
`_meta.parse_confidence` marks every one of them.

**English only** for the section parsers. A profile in another locale degrades
field by field to null with a warning rather than failing. Identity fields carry
locale variants, so names and headlines survive where section text does not.

**Entry boundaries differ per card.** Experience groups by navigation URL,
skills split on font weight, honours on componentKey, recommendations on a one
node lookahead. There is no single rule, and assuming one gives you output that
looks right on whichever card you tested.

**LinkedIn is not deterministic.** The top card is intermittently missing from
the screen response. We retry once and report it in `_meta.partial_fields` if it
is still absent, rather than quietly returning a URL slug as someone's name.

**Not mapped:** job and school descriptions, hiring and open to work badges,
connection degree, mutual connections. Company industry and website live on the
company page, which is a separate fetch. Professional email is not a gap in our
parsing, LinkedIn never exposes it.

**This will break.** Component ids are generated from LinkedIn's build and the
client version is pinned. Both drift.

---

## Cost

A full profile is 8 upstream requests. The default fetch uses 6. With
`?fields=experience` it is 2.

The demo caps itself at 3 requests per minute and 20 profiles per day. Those
numbers exist because the API key is public and every request lands on one real
LinkedIn account. Requests carrying your own session via `X-LI-AT` are exempt,
since that is your account and your risk.

Every request **the API itself makes** is recorded, method, URL, status and
size, with credentials reduced to a truncated hash, in
`docs/evidence/request-log.jsonl`.

Four of the recon scripts predate that logger and call LinkedIn directly
without it: `check_profileview_410.py`, `check_tls_fingerprint.py`,
`compare_endpoints.py` and `probe_rsc_endpoint.py`. Their findings are written
up in `docs/evidence/` instead, but they are not in the request log, so the
log is a complete record of the service and not of every request ever sent
during development.

---

## Going deeper

* **[ARCHITECTURE.md](ARCHITECTURE.md)** is the map of the codebase: what each
  module does, why the parser is shaped the way it is, and where to start if you
  want to change something.
* **[METHODOLOGY.md](METHODOLOGY.md)** is the real writeup. The 410, the RSC
  wire format, the bugs that returned confident wrong answers, the corrections
  we had to make, and a comparison against every other public solution to this
  same problem.
* **[COVERAGE.md](COVERAGE.md)** has per field fill rates and latency from a
  live run.
* **[DEPLOYMENT.md](DEPLOYMENT.md)** covers hosting.
* **[docs/evidence/](docs/evidence/)** has the raw findings, including the
  endpoint comparison and the survey of eleven other submissions.

The code is commented at the level of why rather than what, so reading
`app/linkedin/rsc_parser.py` and `app/normalize/mapper.py` will tell you more
about the protocol than any summary here. ARCHITECTURE.md says which file to
open for what.

---

## Legal

> [!CAUTION]
> **This violates LinkedIn's User Agreement.** Not a grey area. Read this
> section before running it against anything you care about.

LinkedIn litigates. Proxycurl, a funded business built on exactly this, shut
down in 2025 citing LinkedIn's legal resources, ending in a permanent injunction
requiring deletion of all scraped data. The reference open source client
(`tomquirk/linkedin-api`) has gone private and its forks are unmaintained.

The case law is unsettled rather than favourable. *hiQ v. LinkedIn* established
that scraping public data is not a CFAA violation, but this API authenticates
with a member session, which puts it under the User Agreement as contract. hiQ
lost that part.

Profiles of EU and UK residents are personal data under GDPR. `GET /v1/cache`
and `DELETE /v1/cache/{public_id}` are a good faith answer to the erasure right,
not a compliance claim.

Built for a technical evaluation, run against a handful of profiles, not
operated at scale.

On what this repository contains: the bulk captures are not here. No HAR files,
no raw API responses, no cache database, no request log. All of it is
gitignored, and CI fails the build if any of it appears.

It does contain small illustrative examples drawn from public figures' public
profiles: the sample response above uses Bill Gates, a few names appear in
documentation and tests, and `seeds.txt` lists six profile URLs. The one
committed test fixture is a real captured response with the identity replaced
by a person who does not exist. If you are one of the people named and would
rather not be, `DELETE /v1/cache/{public_id}` clears the live demo and an issue
gets it out of the repository.

Do not deploy this commercially without legal advice.
