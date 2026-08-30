# Survey of 11 submissions to the same challenge

Public repositories, plus live probes of every deployment that answered. Probed
against `https://www.linkedin.com/in/williamhgates/` — a canonical public
profile, deliberately not one of ours.

## Approach taken

| Approach | Count | Repos |
|---|---|---|
| Voyager `dash/profiles` + `FullProfileWithEntities-101` | 7 | sid1651, Shoryamishra61, arunsingh2004, Rhushya, Viswesh934, NEhIL06, sanjay-mali |
| SDUI / React Flight (RSC) | 3 | vishkrish200, aditya-majhi, **this project** |
| Third-party scraping API (Apify) | 1 | raghavv483 |
| Public JSON-LD fallback (secondary) | 1 | lowkeyarhan |

**The dominant approach is the decorated Dash endpoint.** Seven of eleven use
the identical decoration id. This project is in a minority of three that
reverse-engineered the RSC layer instead.

One submission does not reverse-engineer LinkedIn at all: it calls Apify's
`harvestapi/linkedin-profile-scraper`, and says so plainly. That is a defensible
engineering choice but a different exercise from the one set.

## Live probe results

| Repo | Deployment | Result for `williamhgates` |
|---|---|---|
| **vishkrish200** | Cloud Run, no key | **200, real data** — name, headline, location, about, 3 experience, 2 education, 4 images |
| **NEhIL06** | Vercel, no key | **200, real data** — 39 flat fields |
| **Rhushya** | Render, no key | 200, but **fabricated**: `"source":"demo-fallback"`, returns "Aarav Mehta" for Bill Gates |
| sid1651 | Vercel | 401, no token published; `/ready` reports `linkedInSession:false` |
| Shoryamishra61 | Vercel | 401, no key published |
| aditya-majhi | Render | 404 on probed routes |
| raghavv483 | Render | needs an Apify token |
| Viswesh934 | Render | key published (`test-challenge-api-key-2026`), URL not located |
| lowkeyarhan, arunsingh2004, sanjay-mali | none | no public demo |

**Two of eleven deployments returned real profile data to an anonymous caller.**

### One returns invented data

Rhushya's deployment answers 200 for any profile with a fabricated person under
a `demo-fallback` source flag. It is labelled in the payload, so it is not
concealed — but a caller passing Bill Gates's URL receives a fictional
"Aarav Mehta" with a 200 status. Failing closed with a 503 is the safer
behaviour; this project does that.

## What the two working demos have that this project does not

**NEhIL06** matched the **PhantomBuster schema the challenge links as its
example** — 39 flat fields with exactly those names. Fields it returns that this
project does not:

* `companyIndustry` — "Non-profit Organization Management"
* `linkedinCompanySlug` / `linkedinSchoolCompanySlug` — **vanity** slugs
  (`gates-foundation`, `lakeside-school`) where this project has numeric ids
* `linkedinIsHiringBadge`, `linkedinIsOpenToWorkBadge`
* `connectionsUrl`, `mutualConnectionsUrl`
* `linkedinProfileImageUrn`

Its trade is depth: **one** current job and **one** education record, where this
project and vishkrish200 return the full arrays.

**vishkrish200** is the closest comparison — same RSC approach, deployed,
working. Its output for this profile matches this project's field for field,
with one difference: it returns `warnings: []` alongside empty `skills`,
`certifications` and `languages`, giving a caller no way to distinguish "this
member has none" from "we could not fetch them".

**lowkeyarhan** has a technique nobody else does: a `PublicLdJsonStrategy` that
parses `<script type="application/ld+json">` from the public profile page as a
fallback, degrading to `"partial": true` when the authenticated path fails.
**That path needs no session at all** — which is precisely the failure mode that
took down three of the deployments above.

## What this project has that none of them do

* **Pagination past LinkedIn's card caps** (`?complete=true`): 26 skills where
  the card returns 2, 7 certifications where it returns 2. No other submission
  addresses truncation.
* **Recommendations with bodies**, honours, volunteer causes, courses.
* **A pre-seeded cache verified to serve with the session disabled** — the exact
  state that broke sid1651, Shoryamishra61 and Rhushya.
* **Caller-supplied sessions** (`X-LI-AT`), never logged, cached or persisted.
* **Cache transparency and erasure** (`GET`/`DELETE /v1/cache`).
* **A daily live-fetch budget** protecting the underlying account.
* **`_meta.parse_confidence`** distinguishing typed values from regex-recovered
  ones, and `coverage` distinguishing "empty" from "truncated".
* 221 offline tests with network access blocked at the boundary.

## Honest reading

On **data-layer choice**, the majority is right and this project took the harder
road: one decorated Dash request returns typed values where the RSC path needs
six requests and a regex layer. That is documented in METHODOLOGY §20.

On **everything after the fetch** — truncation, provenance, failure behaviour,
session protection, operating without a session — this project is ahead of all
eleven, and the gap is largest exactly where the deployments actually failed.

The two gaps worth closing, in order of cost:

1. **A flat PhantomBuster-shaped output.** The challenge links that schema as its
   example; NEhIL06 matched it. This project already holds every field it needs
   except `companyIndustry` and vanity company slugs.
2. **A public JSON-LD fallback.** Works with no session, which is the failure
   mode that disabled most of the field.
