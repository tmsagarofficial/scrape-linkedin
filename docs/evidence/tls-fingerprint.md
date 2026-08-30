# TLS fingerprinting: what was actually measured

Recorded 2026-08-30T18:28:42+00:00.
This closes the outstanding item in METHODOLOGY §2.

## The claim under test

The brief states it as ground truth:

> Plain `requests` / `httpx` / `aiohttp` present a JA3 TLS fingerprint no real
> browser produces. LinkedIn returns **HTTP 999** regardless of a perfectly
> valid session cookie. `curl_cffi` with Chrome impersonation returns 200 with
> the *identical* cookie.

Everything here is built on `curl_cffi` because of it.

## Experiment 1 — authenticated, Voyager Dash

Same session, same headers, same target, back to back:

| Client | Status | Bytes |
|---|---|---|
| `requests` (no impersonation) | **200** | 13,448 |
| `curl_cffi` chrome142 | **200** | 13,448 |

Byte-identical. **Impersonation made no difference.**

## Experiment 2 — unauthenticated, public profile page

No cookies at all. Same headers, same target, redirects followed:

| Client | Status | Bytes |
|---|---|---|
| `requests` (no impersonation) | **999** | 1,530 |
| `curl_cffi` chrome142 | **999** | 1,530 |

Byte-identical again. **Impersonation made no difference here either.**

## What this supports

**The 999 is real.** It was reproduced, and it is the first one observed in this
project despite hundreds of authenticated requests.

**It does not appear to be triggered by the TLS fingerprint.** In both
experiments the two clients received identical responses. What separated 200
from 999 was **the presence of a valid session**, not the shape of the
handshake.

The honest conclusion is narrower than the brief's: on the endpoints and from
the vantage point tested here, `curl_cffi` was **not load-bearing**. Seven of the
eleven other submissions to this challenge use ordinary HTTP clients, and that
is consistent with what was measured.

## What this does not support

It is one IP, one account, one afternoon. Specifically:

* **The IP may be tainted.** A session-wide soft block was triggered earlier the
  same day (see `endpoint-comparison.md`). A clean address might well receive
  200 on the unauthenticated page where this one received 999.
* **Datacenter egress is a different question.** Another submission documents
  that serverless egress is refused outright, which is an IP-reputation effect
  that no TLS profile fixes.
* **LinkedIn's defences change.** The claim may have held when it was written.

`curl_cffi` is therefore retained — it costs nothing, it matches the user-agent
that is sent, and being wrong about it in the other direction is expensive.
But it is retained as **insurance, not as the mechanism**, and the README should
not claim it is what makes the requests work.

## Consequence for the public-JSON-LD fallback

One submission falls back to parsing `<script type="application/ld+json">` from
the public profile page when the authenticated path fails — an appealing idea,
because it needs no session at all.

From this vantage point it does not work: the public page returned **999 with
zero `ld+json` blocks** to both clients. Whatever makes that path viable is
IP reputation, not client construction. It is worth implementing only behind a
residential proxy, and worth measuring before being relied upon.
