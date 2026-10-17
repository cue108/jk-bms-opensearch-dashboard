# Security notes

This stack is built for a **trusted home LAN**. Read this before you expose
anything beyond it — battery telemetry is intimate data: it reveals when you
are home, what runs, and your overall energy usage pattern.

## What runs where (defaults)

| Surface | Default | Exposure assessment |
|---|---|---|
| OpenSearch `:9200` | security plugin **disabled** | LAN-only. Unauthenticated read/write of all history. Never internet-facing. |
| Collector `:8099` | no auth | full JSON state incl. device serial/settings. LAN-only. |
| Web UI `:8098` | no auth | status + history for anyone who can reach it. LAN-only. |

## Before any public exposure

1. **Keep OpenSearch and the collector strictly LAN-private.** They are APIs,
   not websites.
2. **Front the web UI with a reverse proxy** that does TLS + authentication —
   e.g. Caddy/nginx/Traefik basic auth, Authelia/Keycloak, or simplest of
   all: a WireGuard/Tailscale VPN and no public port at all.
3. If you enable OpenSearch security (`DISABLE_SECURITY_PLUGIN=false`),
   configure admin credentials + TLS before opening any port — do not run the
   demo configuration on the internet either.
4. Watch for **denial of wallet**: an exposed OpenSearch can be filled with
   junk data in minutes.

## Built-in hardening of the frontend

The frontend intentionally serves only fixed routes. Static files go through
path containment:

```python
f = (WEB / rel).resolve()
if f.is_file() and f.is_relative_to(WEB.resolve()):   # else -> 404
```

so raw `../` (and encoded variants, which the HTTP layer decodes first) can
never escape `web/` — attempts like `/web/../../.ssh/id_rsa` return 404.
Additional properties:

- only `GET`/`HEAD` are implemented (other methods → 501; TRACE → no echo)
- no directory listing
- neutral `Server:` header (framework/version not advertised)
- no third-party CDN calls — everything is vendored (works air-gapped, and
  no supply-chain surface in the browser)

## The collector is read-only by design

BLE traffic is limited to *requesting* two frame types and subscribing to
notifications. There is no code path that writes BMS settings — so a bug in
this service cannot reconfigure your battery protection limits.

## Incident checklist (if you did expose something)

- `docker compose down` stops everything immediately
- OpenSearch data lives in the named volume `opensearch-data` — delete it to
  purge history
- Rotate nothing inside this stack (no credentials by default) — rotate at
  the reverse proxy / VPN layer
