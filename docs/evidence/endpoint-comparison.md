# Endpoint comparison

Target: `cooktim`. One request per variant, same session, same target, run back to back.

| | Endpoint | Client | Status | Result | Bytes | Time |
|---|---|---|---|---|---|---|
| **A** | voyager identity/dash/profiles | requests (no impersonation) | `200` | OK — JSON | 13,448 | 0.8s |
| **B** | voyager identity/dash/profiles | curl_cffi chrome142 | `200` | OK — JSON | 13,448 | 0.8s |
| **C** | voyager profileView (legacy) | curl_cffi chrome142 | `410` | GONE — HTTP 410 — endpoint retired | 37 | 0.5s |
| **D** | SDUI screen (this project) | curl_cffi chrome142 | `200` | OK — RSC flight stream | 731,133 | 2.0s |

## What each variant tests

* **A** is what the other implementations of this brief do: the Dash endpoint with an ordinary HTTP client and no TLS impersonation.
* **B** isolates the endpoint from the client — same request, Chrome TLS fingerprint.
* **C** is the control: the endpoint this project found retired.
* **D** is this project's path.

## Reading the result

`dash/profiles` answers **with and without** TLS impersonation. `curl_cffi` is therefore not load-bearing for this endpoint, and the other implementations' simpler stacks are justified.

Entity types in variant A (1 entities):

* `Profile` × 1

Entity types in variant B (1 entities):

* `Profile` × 1


---

## Addendum: the comparison tripped a soft block

Recorded 2026-08-29T18:41:39+00:00.

Roughly six minutes after the comparison above ran cleanly, **every** request
began returning a redirect loop — the endpoint 302-ing to its own URL — for
every profile and on both endpoints:

| Probe | Before | After |
|---|---|---|
| `dash/profiles` (cooktim) | 200, 13,448 B | redirect loop |
| SDUI screen (cooktim) | 200, 731,133 B | redirect loop |
| SDUI screen (williamhgates) | 200, 734,313 B | redirect loop |
| SDUI screen (tmsagarofficial) | 200 | redirect loop |

Waiting 45 seconds did not clear it. It is session- or account-wide, not
specific to a profile or an endpoint.

### The mechanism: redirect loops amplify requests ~90x

This is the part worth carrying forward. A self-redirect is not one failed
request. With libcurl's default ceiling of 30 redirects, one logical call
becomes **30 HTTP requests**. The client then retried transport failures three
times, so a single `fetch_screen` could issue **~90 requests to LinkedIn** while
reporting itself as one failure.

A probe of four `decorationId` values — four logical requests — plausibly
generated several hundred. That is scraping-shaped traffic produced by a script
that believed it was being careful, and it is a far more likely trigger for the
block than the ~14 deliberate requests made in the same window.

### What was changed as a result

* `MAX_REDIRECTS = 5`, down from libcurl's default of 30.
* A self-redirect now raises `RedirectLoop` immediately and is **never
  retried** — retrying a loop cannot succeed and multiplies the damage.
* The condition is logged distinctly rather than as a generic transport error,
  so it is visible in the request log.

Failure time for a looping request went from roughly 20 seconds to 2.5.

### What this says about the daily budget

The existing ceiling — 40 profiles/day, about 240 upstream requests — was set by
reasoning about what a human plausibly browses. This episode suggests the
binding constraint is **burst rate**, not daily total: this project made over
300 logged requests across several days without issue, then tripped a block
inside six minutes.

Both limits are therefore necessary, and the per-minute one matters more than
first assumed. For a public deployment the sensible defaults are lower than the
current ones, and the account used should be one nobody minds losing.

### Effect on the comparison's conclusions

None. Variants A–D all completed inside one minute, before the block, and were
independently reproduced with a fresh session. The findings stand:
`dash/profiles` answers a plain HTTP client, carries identity fields only, and
`profileView` is retired.
