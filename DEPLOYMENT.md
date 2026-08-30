# Deployment

The image is provider-neutral. What follows is what any host must satisfy, then
the commands for a few specific ones.

## Requirements, in order of how badly they bite

### 1. Egress IP — measured, and less of a problem than expected

An earlier version of this document claimed egress IP quality was the deciding
factor and that a residential proxy was effectively required. **That was tested
against other deployments of this same challenge and does not hold.**

Two competing implementations were probed with an obscure profile, one unlikely
to be in any cache:

| Deployment | Platform | Result |
|---|---|---|
| RSC-based, `asia-south1` | Cloud Run | **200**, live, 7 experience entries, 5.4 s |
| Voyager Dash-based | Vercel | **200**, live, 1.1 s |

Both fetched live from datacenter IPs, with **no proxy**. The Cloud Run
implementation's own documentation contains no mention of proxies, residential
egress or IP rotation; its protections are all session-side — cooldowns after
429/999/checkpoint signals, a bounded queue, pacing and a circuit breaker.

**So a residential proxy is optional, not required.** `PROXY_URL` remains
supported and is still worth using if you have one, but a deployment without one
is expected to work.

What actually took the other deployments down was **session expiry**, not
blocked IPs: one reports `linkedInSession:false` at its own readiness endpoint,
another falls back to serving fabricated data. Those are dead cookies.

**The binding constraint is session freshness and request pacing.** Plan for
those, not for IP reputation.

### 1b. Region still matters, for a different reason

Prefer a region close to where the session cookie was issued. A session created
in India and used from Oregon is a visible inconsistency, independent of whether
the IP is residential. `asia-south1` (Mumbai) is the closest option to an Indian
session, and is where the one proven-working RSC deployment runs.

### 2. Do not scale to zero

A cold start makes the demo look broken to a reviewer clicking a link. Keep at
least one instance warm. This rules out the free tier on several hosts, or
requires a keep-warm ping.

### 3. Persist the cache

`CACHE_PATH` should point at a mounted volume. Without one the cache is rebuilt
on every deploy, which costs LinkedIn requests and — more importantly — removes
the stale-if-error fallback that keeps the API answering once the session dies.
A single writer means a plain volume is enough; no managed database is needed.

### 4. HTTPS

Required. Every host below terminates TLS for you.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `LI_AT` | yes | Session cookie. The primary auth path in practice. |
| `JSESSIONID` | yes | Supplies the `csrf-token` header. |
| `API_KEY` | yes | Client auth. Published in the README on purpose. |
| `PROXY_URL` | strongly recommended | Residential egress for LinkedIn traffic. |
| `CACHE_PATH` | recommended | Point at a mounted volume. |
| `IMPERSONATE` | no | TLS profile; defaults to `chrome142`. |
| `CACHE_TTL_SECONDS` | no | Default 86400. |
| `RATE_LIMIT_PER_MIN` | no | Outbound cap; default 20. |

`LI_USER` / `LI_PASS` exist for the login flow but are expected to fail against
LinkedIn's JavaScript challenge (AGENTS.md §3). Set `LI_AT` directly.

**Set every one as a secret**, never in a committed config file.

## Provider recipes

### Fly.io

```bash
fly launch --no-deploy --region bom      # bom = Mumbai
fly volumes create data --size 1 --region bom
fly secrets set LI_AT="..." JSESSIONID="..." API_KEY="..." PROXY_URL="..."
fly deploy
```

In `fly.toml`, mount the volume and keep a machine warm:

```toml
[mounts]
  source = "data"
  destination = "/data"

[http_service]
  auto_stop_machines = false
  min_machines_running = 1
```

### Render

Docker environment, add a Disk mounted at `/data`, set `CACHE_PATH=/data/cache.db`.
Instance type must be paid — the free tier sleeps.

### Railway

Deploy from the Dockerfile, add a Volume at `/data`, set variables in the
dashboard. `PORT` is injected automatically.

### Google Cloud Run — the recommended target

`asia-south1` is closest to an Indian session, and is where the one
independently-verified working RSC deployment of this challenge runs.

**Step 1 — seed the cache and bake it into the image.**

Cloud Run has no volume to mount, so a cache written at runtime dies with the
instance. Baking the seed in means every cold start can answer for the seeded
profiles even when the LinkedIn session has expired — the state most comparable
deployments failed in.

```bash
python3 scripts/seed_cache.py            # writes cache.db
python3 scripts/seed_cache.py --verify   # prove it serves with LI_AT unset
cp cache.db seed-cache.db                # picked up by the Dockerfile
```

`seed-cache.db` is gitignored, so it never reaches the repository — it exists
only in the image you build.

**Step 2 — store the session as secrets, never as env vars in the console.**

```bash
printf '%s' "$LI_AT"      | gcloud secrets create li-at      --data-file=-
printf '%s' "$JSESSIONID" | gcloud secrets create jsessionid --data-file=-
printf '%s' "demo-key"    | gcloud secrets create api-key    --data-file=-
```

**Step 3 — deploy.**

```bash
gcloud run deploy scrape-linkedin \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --min-instances 1 \
  --memory 512Mi \
  --timeout 120 \
  --set-secrets LI_AT=li-at:latest,JSESSIONID=jsessionid:latest,API_KEY=api-key:latest \
  --set-env-vars RATE_LIMIT_PER_MIN=3,DAILY_LIVE_FETCH_BUDGET=20
```

Why each flag matters:

* `--min-instances 1` — no cold start, and the SQLite cache survives between
  requests instead of being rebuilt.
* `--timeout 120` — a full profile is several upstream requests; the 60 s
  default can cut a `?complete=true` fetch short.
* `--allow-unauthenticated` — the API has its own `X-API-Key`; Google IAM on top
  would stop a reviewer from calling it at all.
* Low pacing limits — the traffic is attributed to one real LinkedIn account.

**Step 4 — rotating the session later.** The cookie will expire before the
deployment does:

```bash
printf '%s' "$NEW_LI_AT" | gcloud secrets versions add li-at --data-file=-
gcloud run services update scrape-linkedin --region asia-south1
```

### Plain VPS

```bash
docker build -t linkedin-profile-api .
docker run -d --restart unless-stopped -p 8080:8080 \
  -v /srv/linkedin-cache:/data --env-file .env \
  linkedin-profile-api
```

Put a TLS terminator (Caddy, nginx, Traefik) in front. A small VPS in the right
region can be a better answer than a PaaS here, because the egress IP is
residential-adjacent rather than a known cloud range.

## Verifying a deployment

```bash
curl https://<host>/health

curl -H "X-API-Key: <key>" \
  "https://<host>/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

Check in this order:

1. `/health` returns `session: configured`
2. A **seeded** profile returns 200 with `_meta.source: "cache"`
3. An **unseeded** profile with `?refresh=true` returns 200 with
   `_meta.source: "live"` — this is what actually proves live fetching works
4. `/docs` renders

Test from mobile data or an incognito window, not the machine you deployed
from. A misconfigured proxy frequently still works from the developer's own
network and fails everywhere else.
