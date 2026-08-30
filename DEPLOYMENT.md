# Deployment

The image is provider-neutral. What follows is what any host must satisfy, then
the commands for a few specific ones.

## Requirements, in order of how badly they bite

### 1. Egress IP quality — the one that actually decides this

LinkedIn blocks datacenter ASNs. A container on any mainstream PaaS egresses
from exactly such a range, so the platform's own IP will usually fail
regardless of how valid the session cookie is.

Two workable arrangements:

* **Residential proxy, sticky session**, with `PROXY_URL` set. LinkedIn traffic
  is routed through it; the API's own responses serve direct, so latency is paid
  only on the upstream fetch.
* **Egress geographically near the session's origin.** A session created in
  India and used from a US datacenter is a visible inconsistency. Prefer a
  region close to where the cookie was issued.

The proxy matters more than the provider. Choose the provider on other grounds.

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

### Google Cloud Run

```bash
gcloud run deploy linkedin-profile-api \
  --source . --region asia-south1 \
  --min-instances 1 \
  --set-secrets LI_AT=li-at:latest,JSESSIONID=jsessionid:latest,API_KEY=api-key:latest
```

Cloud Run's filesystem is ephemeral, so either accept a per-instance cache or
mount GCS via Cloud Storage FUSE. `--min-instances 1` is required.

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
