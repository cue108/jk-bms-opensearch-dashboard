# Architecture

## Components

| Service | Port | Role |
|---|---|---|
| `collector` (backend) | 8099 | BLE poll loop, state API, per-minute OpenSearch indexing |
| `web` (frontend) | 8098 | status page + history charts, proxies `/api/state` to the collector, queries OpenSearch directly |
| `opensearch` | 9200 | time-series store (single node, security plugin disabled for LAN use) |
| `opensearch-init` | — | one-shot job that installs the `jkbms` index template |

All inter-service traffic happens on the Compose network
(`http://opensearch:9200`, `http://collector:8099`); only the three ports above
are published to the host.

## Data flow

1. **BLE (~1 Hz)** — the collector connects read-only to the JK BMS and
   subscribes to the notify characteristic. Frame types handled:
   - `0x96` cell info (voltages, currents, SOC, temperatures, status flags)
   - `0x97` device info (model, hw/sw, serial)
   - settings frame (all configured protection limits)
2. **State** — a thread-safe in-memory dict, served as JSON at
   `GET /api/state` (also `GET /api/health` → `ok` while frames stream).
3. **Indexing (1/min, minute-aligned)** — `os_push.push_state()` flattens the
   state into one document: per-cell fields `v1…v8`, derived `delta_mv` /
   `worst_deviation_mv` / `charge_rate_c` / `est_runtime_h`; stale snapshots
   are flagged (`stale: true`) so gaps stay visible.
4. **Index scheme** — daily indices `INDEX_PREFIX-YYYY.MM.DD` (date computed
   in `INDEX_TZ`), 1 shard, 0 replicas, strings mapped as keywords via the
   template in `opensearch-config/index-template.json`.

## Endpoints

### Collector (backend)

```
GET /api/state    full JSON state (device, live, cells, pack, soc, temps, status, settings)
GET /api/health   200 with "ok" (frames streaming) or "no-stream"
GET /             plain-text API landing (the UI lives in the frontend)
```

### Frontend

```
GET /              portal shell (tabs: Status | History; remembers #hash)
GET /live          live status page (JS polls /api/state, proxied to the collector)
GET /history       history charts page (uPlot)
GET /api/series    JSON time series: ?range=1h|6h|24h|7d|30d|90d|180d
                   -> {t:[ms…], series:{field:[…]} , interval_label, range_ms}
GET /api/state     proxied to the collector (same-origin for /live)
GET /api/health    proxied to the collector
GET /web/*         vendored static assets (path-contained: resolved paths
                   must stay inside web/, otherwise 404)
```

## Chart engine notes (why uPlot)

Requirements: cursor crosshair + all-series readout at cursor height +
**cursor synchronized across charts** + y-axes zoomed to the data range.

- uPlot provides all four natively; cross-chart sync is one line:
  `cursor: { sync: { key: "…" } }` (same key on every chart; no manual
  pub/sub wiring needed).
- Dashboard engines we evaluated (OpenSearch-Dashboards / Kibana forks) do not
  offer a cross-panel cursor at all in relevant releases, and their
  Vega-Lite selection semantics (`empty: "none"`) were observed to fail
  open — rules visible at rest — which makes a clean crosshair panel
  impossible there. Hence the self-built slim UI.

## Design constraints

- **Read-only**: the code base contains no BLE write except *requesting*
  frames (GATT write of the two request commands on the notify characteristic).
  No settings are ever modified.
- **Single central**: the BMS accepts one connection; the collector and the
  phone app are mutually exclusive while it runs.
- **Failure isolation**: BLE flakiness is caught and retried with backoff;
  the HTTP state API stays up regardless; indexing failures log + retry next
  minute. A missing `OPENSEARCH_URL` simply disables indexing.
