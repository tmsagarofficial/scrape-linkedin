# Coverage report

Generated 2026-08-29T15:37:18+00:00 by `scripts/coverage_report.py`.

**Sample: 6 profiles.** Mode: default (cards).

> This is a small sample, and the numbers below should be read as indicative rather than as rates. Each profile costs several authenticated upstream requests, so the survey is deliberately capped; AGENTS.md §10 asks for ~50, which is a request budget decision rather than a technical limit.

## Outcome

| | Count |
|---|---|
| Fetched successfully | 6 |
| Failed | 0 |

## Latency

End-to-end fetch, all sections, excluding mapping.

| Percentile | Seconds |
|---|---|
| p50 | 4.96 |
| p95 | 12.62 |
| max | 12.62 |
| mean | 6.44 |

## Field fill rate

Share of **successfully fetched** profiles where the field was populated. A low rate is not necessarily a defect — many members genuinely have no certifications — so the mean count is shown alongside it.

| Field | Filled | Rate | Mean count |
|---|---|---|---|
| `name` | 6/6 | 100% | — |
| `headline` | 6/6 | 100% | — |
| `location` | 6/6 | 100% | — |
| `about` | 4/6 | 67% | — |
| `follower_count` | 4/6 | 67% | — |
| `experience` | 6/6 | 100% | 4.5 |
| `education` | 5/6 | 83% | 1.7 |
| `skills` | 4/6 | 67% | 1.3 |
| `certifications` | 2/6 | 33% | 0.5 |
| `languages` | 1/6 | 17% | 0.3 |
| `courses` | 0/6 | 0% | 0.0 |
| `honors` | 1/6 | 17% | 0.2 |
| `volunteer_causes` | 0/6 | 0% | 0.0 |

## Truncation

Sections LinkedIn capped. Re-run with `--complete` to page the full lists.

| Section | Profiles truncated |
|---|---|
| `profile-card-experience` | 6/6 |
| `profile-card-skills` | 4/6 |
| `profile-featured-show-all-button` | 2/6 |
| `profile-card-education` | 2/6 |
| `publication-see-all-publications` | 2/6 |
| `volunteer-exp-see-all-button` | 1/6 |
| `honors-see-all-button` | 1/6 |
| `profile-card-licenses-and-certifications` | 1/6 |

## Per profile

A blank scalar cell means the field was absent for that profile. That is shown per profile rather than only as a rate, because an aggregate cannot distinguish a parser gap from a member who genuinely has no location set.

| Profile | Time | Loc | Foll | About | Exp | Edu | Skills | Certs | Langs | Warn |
|---|---|---|---|---|---|---|---|---|---|---|
| `williamhgates` | 4.3s | yes | yes | yes | 3 | 2 | 0 | 0 | 0 | 1 |
| `satyanadella` | 6.4s | yes | yes | yes | 5 | 2 | 0 | 0 | 0 | 2 |
| `reidhoffman` | 5.8s | yes | yes | yes | 5 | 3 | 2 | 0 | 0 | 8 |
| `tmsagarofficial` | 12.6s | yes | — | — | 5 | 1 | 2 | 2 | 0 | 3 |
| `rajshamani` | 4.6s | yes | yes | yes | 4 | 0 | 2 | 0 | 2 | 2 |
| `cachemoney` | 5.0s | yes | — | — | 5 | 2 | 2 | 1 | 0 | 4 |

## Method

Each profile is fetched through the same client the API uses, then mapped through the same mapper, so these numbers reflect what a caller would actually receive. Every request is recorded in `docs/evidence/request-log.jsonl`.

A field counts as filled only if it is non-empty after mapping. `name` additionally does not count when it fell back to the URL slug, which is what happens when LinkedIn omits the top card.
