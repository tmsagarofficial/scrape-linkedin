# Architecture

Orientation for anyone reading or changing this codebase, human or agent.
Written to answer "where do I look for X" and "why is it built this way",
because the second question explains most of the odd decisions in the first.

[README.md](README.md) covers what the API does.
[METHODOLOGY.md](METHODOLOGY.md) covers how the protocol was reverse
engineered. This file is about the code.

---

## Shape of the thing

Data comes from two LinkedIn sources, gets parsed into a flat stream of tagged
text, is assembled into a schema, then cached and served.

```
   HTTP request
        |
   app/main.py ................. routes, auth, fallback chain, error taxonomy
        |
        +-- app/linkedin/urls.py ......... URL to public_id, 400 on non-profiles
        +-- app/cache.py ................. SQLite, stale-if-error
        +-- app/budget.py ................ daily ceiling on live fetches
        |
   app/linkedin/client.py ...... orchestrates the upstream calls
        |
        +-- Voyager Dash  --> app/linkedin/voyager.py ..... typed identity fields
        +-- SDUI screen   --\
        +-- SDUI cards    ---> app/linkedin/rsc_parser.py . RSC wire format
        +-- SDUI pagers   --/                               to tagged text nodes
        |
   app/normalize/mapper.py ..... text nodes to schema, section by section
        |
        +-- app/linkedin/text_parsers.py . regex over display strings
        +-- app/schemas.py ............... Pydantic models, the public contract
        |
   JSON response + _meta
```

Everything routed through `client.py` also writes to
`app/linkedin/request_log.py`, an append-only audit trail. Four older recon
scripts bypass it; see the note under that module.

---

## Modules

### The parsing core

**`app/linkedin/rsc_parser.py`** (498 lines) is the piece worth reading first.
LinkedIn serves profile content as React Server Components flight streams, not
JSON: newline-delimited `<hex_id>:<payload>` records that reference each other.
This turns that into an ordered list of `TextNode`, each tagged with the section
that rendered it and the entity it links to.

Four things here caused real bugs and are commented as such:

* Records arrive out of document order, so resolution is two-pass.
* Ids are hexadecimal. Coercing them to `int` mis-resolves about one reference
  in ten, silently.
* There are two reference forms, `$L35` and `$35`. Handling only the first
  parses every record without error and extracts nothing.
* A response can have several unreferenced roots. Walking only record `0`
  returns the recommendations tab headers and discards the recommendations.

Text extraction uses a structural rule rather than a list of prop names:
**rendered text is always a list element, metadata is always a scalar dict
value.** That survives LinkedIn renaming props, which a list does not.

**`app/linkedin/text_parsers.py`** (254 lines) recovers structure from strings
LinkedIn already formatted for humans, like `"Full-time · 4 yrs 1 mo"`. Every
function returns partial results and never raises. Note the separator table at
the top: experience uses a hyphen and education an en dash, and matching only
one silently drops every date on the other side.

**`app/linkedin/voyager.py`** (211 lines) parses the Voyager Dash identity
record. Smaller and simpler because that endpoint returns typed JSON. It
supplies things the RSC payload has no representation for at all: locale
variants, signed-image expiry, an authoritative first and last name.

### Assembly

**`app/normalize/mapper.py`** (1050 lines, the biggest file) turns tagged text
nodes into the public schema. It is large because **every card delimits its
entries differently**, and each rule is documented where it is used:

| Section | How entries are separated |
|---|---|
| Experience | navigation URL, then date-range positions within a group |
| Education | navigation URL |
| Skills | `fontWeight`, since credentials sit beside skills as siblings |
| Honours | `componentKey` |
| Recommendations | one-node lookahead on the connection-distance line |
| Certifications | arrival of a second plain line after a name |

If you are adding a section, expect to work out its boundary rule from a real
capture rather than reusing one of these.

**`app/schemas.py`** (274 lines) is the public contract. Pydantic models, and
the only file that should change when the response shape changes. `Meta` carries
`parse_confidence` and `coverage`, which are how a consumer tells an inferred
value from a supplied one and an empty section from a truncated one.

### Transport

**`app/linkedin/client.py`** (774 lines) does all the talking. Header
construction, the reconstructed POST bodies, retries with jittered backoff,
redirect-loop detection, and the section-to-component map. Most of the
constants near the top were established by measurement and the comments say
which.

**`app/linkedin/request_log.py`** (147 lines) appends every request made
through `client.py` to `docs/evidence/request-log.jsonl`. Credentials are
reduced to a truncated SHA-256 before writing. Transport failures are logged
too, which they were not originally, and an audit log that keeps only successes
is worse than none.

Four recon scripts predate this and call LinkedIn directly without it
(`check_profileview_410.py`, `check_tls_fingerprint.py`, `compare_endpoints.py`,
`probe_rsc_endpoint.py`). Anything new that talks to LinkedIn should go through
`client.py` rather than reaching for `curl_cffi`.

**`app/linkedin/urls.py`** (69 lines) is small but load-bearing: it is the 400
branch of the error taxonomy. A company or jobs URL must be rejected before a
request is spent on it.

### Service

**`app/main.py`** (548 lines) holds the routes, the API-key dependency, the
caller-supplied-session path, and the fallback chain: live, then fresh cache,
then stale cache at any age, then a structured 503. Two rules are enforced here
rather than assumed: never a bare 500, and never a 200 full of nulls.

**`app/cache.py`** (179 lines) is SQLite in WAL mode. `allow_stale` is the
stale-if-error path. `entries()` and `delete()` back the transparency endpoints.

**`app/budget.py`** (100 lines) is a hard daily ceiling on live fetches, stored
in SQLite so a crash loop cannot reset it. It exists because the demo API key is
published and every request lands on one real LinkedIn account.

**`app/config.py`** (141 lines) reads the environment and loads `.env`. Real
environment variables always win over the file, so a deployment injecting
secrets is never overridden.

---

## Tests

230 tests, no network, no credentials.

| File | Tests | Covers |
|---|---|---|
| `test_mapper.py` | 68 | Section mapping, entry boundaries, provenance |
| `test_api.py` | 54 | Error taxonomy, cache behaviour, budget, caller sessions |
| `test_rsc_parser.py` | 29 | Wire format, reference forms, multiple roots |
| `test_text_parsers.py` | 17 | Display-string regex, on real captured strings |
| `test_voyager.py` | 15 | Identity record, images, locale variants |
| `test_config.py` | 8 | `.env` loading and precedence |
| `test_urls.py` | 5 | URL parsing, the 400 branch |

**`tests/conftest.py` blocks `curl_cffi` outright.** A test that reaches the
network fails loudly. This is enforced rather than intended because it was
already violated once: a caller-session test built a client the stub did not
cover and spent two minutes retrying against LinkedIn with a fake cookie. The
suite passed. It was just slow.

The fixture in `tests/fixtures/` is a real captured response with a synthetic
identity substituted in. Structure is byte-for-byte what LinkedIn sent; the
person does not exist.

---

## Scripts

Nothing in `scripts/` is imported by the application. Each one that can reach
LinkedIn refuses to run without credentials and prints its request cost first.

**Recon, kept for reproducibility**
`extract_rsc_from_har.py`, `check_profileview_410.py`, `check_tls_fingerprint.py`,
`probe_rsc_endpoint.py`, `probe_component.py`, `compare_endpoints.py`

**Cache and deployment**
`seed_cache.py` (live), `seed_from_captures.py` (offline, no network),
`export_seed.py` (compact seed for hosts that build from git)

**Safety and reporting**
`scan_secrets.py` (also runs in CI over full history), `scrub_fixture.py`,
`coverage_report.py`, `build_profile_from_captures.py`

---

## Conventions

**Comments explain why, not what.** The what is usually obvious from the code.
The why is often a bug that took an afternoon to find, and those comments are
the most valuable thing in the repository.

**Never fabricate to fill a gap.** A missing field is null with a warning. A
failed section is reported in `_meta`, not silently dropped. One competing
implementation returns an invented person under a `demo-fallback` flag when its
session dies; this one returns 503.

**Distinguish "absent" from "we failed".** That distinction runs through
`_meta.coverage`, `parse_confidence`, `partial_fields` and the null-versus-zero
handling in recommendation counts.

**Degrade per field, not per request.** A locale we cannot parse costs that
field, not the profile.

**Credentials never reach disk or logs.** The request log stores fingerprints.
Caller-supplied sessions are never cached, logged or persisted. `scan_secrets.py`
enforces this over the whole git history in CI.

---

## Where to start

Fixing a parsing bug: get a real capture into `docs/evidence/raw/`, then work
in `rsc_parser.py` and `mapper.py`. `build_profile_from_captures.py` runs the
whole pipeline offline against saved captures, so you can iterate without
touching LinkedIn.

Adding a section: find its component in `docs/evidence/rsc-components.md`, add
it to `SECTION_COMPONENTS` in `client.py`, add a mapping function in
`mapper.py`, then work out its entry boundary from the capture. Do not assume it
matches another section's.

Changing the response: `schemas.py` first, then `mapper.py`, then the tests.

Deploying: [DEPLOYMENT.md](DEPLOYMENT.md).
