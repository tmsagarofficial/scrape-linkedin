# Probing other implementations of the same brief

Three other public submissions were found. Two are deployed. All were probed
against one common target so the comparison is like-for-like.

Probed 2026-08-29T18:12:52+00:00.

**Target: `https://www.linkedin.com/in/cooktim/`** — chosen because Tim Cook is
out-of-network (3rd degree) for essentially any session, which exercises
visibility handling; the profile is rich and stable; and he appears in none of
this project's seeds or validation profiles, so no cache is warm for him.

## What they build on

All three use the same endpoint, and it is **not** the one this project uses:

```
GET /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}
```

That endpoint appears in **none** of the eight browser captures taken for this
project. The flagship web client never calls it, so traffic capture alone could
not have revealed it. Two of the three independently confirm this project's
finding that `identity/profiles/{slug}/profileView` returns **410 Gone**.

## Results against the common target

| | Endpoint | Result |
|---|---|---|
| **A** — `tross-delta.vercel.app` | `POST /api/v1/linkedin/profile` | **401** — `Authorization: Bearer` required, no token published |
| **B** — `tross-linkedin-profile-api.vercel.app` | `GET /v1/profiles?url=` | **401** — `X-API-Key` required, no key published |

Neither returned profile data. No key or token is published in either README,
so a reviewer arriving from the repo cannot exercise either demo.

### A also reports itself as not ready

```
GET https://tross-delta.vercel.app/ready
{"status":"not_ready","checks":{"linkedInSession":false,"apiToken":true}}
```

`linkedInSession: false`. The API token is configured; the LinkedIn session is
not. Even with a token, the deployment could not have served live data at the
time of probing. Its `/health` returns `ok`, which is why the separate
readiness check matters — liveness and usefulness are different questions.

### B documents the reason in its own README

> "sustained extraction from datacenter IPs (serverless egress) is currently
> refused by LinkedIn and fails closed"

and

> "there is no fixture mode to fall back on, so this cookie is the only thing
> standing between the deployed service and real profile data."

This is independent confirmation of the egress analysis in this project's
`DEPLOYMENT.md`: LinkedIn refuses datacenter ASNs, and a serverless platform
egresses from exactly those ranges. B identified the problem correctly and
documented it honestly; without a residential proxy there is no way through it.

## What this changes for this project

**Two things to take seriously.**

1. **`dash/profiles` may still be live.** If it is, it returns *typed* JSON —
   `{"month": 8, "year": 2022}` — where the SDUI path returns
   `"Aug 2022 - Jul 2025"` and must be recovered by regex. Typed data is
   strictly better, and the honest conclusion would be that this project found
   a harder path than necessary. This is testable in one request and should be
   tested rather than argued about.

2. **The claim that the profile surface "migrated to SDUI" is too strong.** The
   flagship *web client* migrated. A Voyager endpoint apparently remains. That
   distinction belongs in METHODOLOGY.

**Three things this project does that none of them do.**

* **A published demo key.** Both of theirs 401 a reviewer with no documented way
  in. This project publishes `demo-key` deliberately, on the reasoning that a
  demo nobody can run is worth nothing.
* **A cache that works without a session.** B states outright it has no fixture
  fallback, so an expired cookie means no data at all. This project pre-seeds
  and verifies serving with `LI_AT` unset — which is also why A's
  `linkedInSession: false` would be survivable here.
* **A caller-supplied session path.** `X-LI-AT` lets a reviewer use their own
  cookies when the demo's have expired. Neither offers this.

**One thing to have an answer ready for.** B's README frames TLS impersonation
as *"safeguard evasion"* and rejects it on principle. This project uses
`curl_cffi` Chrome impersonation, as its brief requires. That is a defensible
difference — but it is a difference of principle, not of capability, and it
should be met with an argument rather than a shrug.


---

## Reading their source: the decoration id

Guessing `decorationId` values produced redirect loops. Their public source has
the working one:

```
decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101
```

Two of the three repositories use exactly this value. The version suffix is not
cosmetic — `-35` and `-46` were tried here and LinkedIn answered each with a
redirect loop rather than an error, so a wrong decoration is indistinguishable
from a block unless you already know the right one.

Both repositories state that with this decoration, **experience and education
arrive embedded in the single response** as a normalized URN graph, resolved by
following ownership references rather than by filtering `included[]` on
`$type`.

### The brief described this response, and it was not recognised

AGENTS.md §2.5 reads:

> Voyager responses nest elements several layers deep. **Filtering `included[]`
> by `$type` returns elements out of order** … The only way to preserve true
> rendered order is to **traverse from the root, resolving URN references level
> by level**.

That is a description of the decorated Dash graph. The brief anticipated the
endpoint and warned about its specific failure mode. What happened here instead:
`profileView` returned 410, and the investigation moved to SDUI without testing
the other Voyager endpoint the brief was describing. The §2.5 principle was
then applied to the RSC flight format, where it also holds — but it was written
for the graph, not for RSC.

### If the decoration works, it is the better path

| | Decorated Dash | SDUI / RSC |
|---|---|---|
| Requests for a full profile | **1** | 1 + N (~6) |
| Payload | ~13 KB identity, more with entities | ~730 KB screen alone |
| Dates | **typed** `{month, year}` | display strings, regex-recovered |
| Locale variants | **yes** | absent |
| Image expiry | **yes** | absent |
| TLS impersonation | **not required** | untested |
| Ordering hazard | URN graph traversal | document-order traversal |

One request, typed values, no regex layer. If `FullProfileWithEntities-101`
returns the entities as described, the honest conclusion is that this project
took a harder route to worse data, and the decorated Dash endpoint should be the
primary path — with the SDUI work retained as a fallback for when Voyager
follows `profileView` into retirement, and as the methodological contribution it
still is.

**This is one request to verify and has not been verified yet.**
