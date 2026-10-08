# Splunk Traffic Dashboard

**English** | [简体中文](README.zh-CN.md)

A standalone web dashboard for Splunk ingest traffic: it queries the Splunk
management REST API (port 8089) for per-index daily **event counts, ingest
volume (license metering), disk write and current disk usage**, and renders
them as an ECharts wall-display page. A built-in **first-run setup wizard**
gets you connected without touching any config file, and the **⚙ Settings**
button lets you change everything later. The UI is bilingual (中文 / English).

![dashboard](docs/img/dashboard.png)

## Features

- **Daily ingest volume** (license_usage raw bytes — the billing metric),
  **daily event counts**, **daily disk write**, and **current disk usage**
  (dbinspect)
- 6 KPI cards, stacked bars, share donut, index × date heatmap, disk usage bars
- Per-index summary table + daily detail table with sortable columns; CSV export
  honors the current filters
- Time ranges: today / yesterday / 7–90 days / custom (default range
  configurable, default 7 days); per-index multiselect; auto refresh every 5 min
- **Index auto-discovery**: a background scanner re-discovers indexes and either
  includes new ones automatically (auto mode, with include/exclude glob
  patterns such as `prod_*`) or surfaces them as a 🆕 top-bar chip with
  one-click tracking (manual mode)
- Full-screen loading overlay with a live query log streamed from the server
  (current step: events / ingest / disk write / dbinspect / record range, with
  date spans)
- Results are cached locally (default 300s) so the wall page stays instant;
  if Splunk is unreachable the last good dataset is served with a stale hint
- **Demo mode with fake data when no Splunk is configured** — explore the
  whole UI before connecting
- Bilingual UI (中文 / English): pick the language in the first setup step or
  in ⚙ Settings; force it per-URL with `?lang=en` / `?lang=zh`

## Quick start

Requirements: Python 3.10+ and a host that can reach your Splunk management
port 8089.

```bash
git clone https://github.com/muzian666/splunk-traffic-dashboard.git
cd splunk-traffic-dashboard

python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# Linux/macOS:
# .venv/bin/pip install -r requirements.txt

# start (on Windows you can also double-click start_dashboard.bat)
.venv\Scripts\python -m traffic_dashboard.server     # Windows
# .venv/bin/python -m traffic_dashboard.server       # Linux/macOS (or ./run.sh)
```

Open <http://127.0.0.1:8091> and follow the wizard.

## First-run setup wizard

On first open (no connection configured) a three-step wizard appears over a
blank page:

![setup wizard](docs/img/setup.png)

1. **Prepare** — pick the UI language; built-in guidance for getting a token
   (Splunk Web → Settings → TOKEN → New Token, or a one-line `curl` against
   `/services/authorization/tokens`), plus notes on 8089 vs 8000 and
   self-signed certificates.
2. **Splunk connection** — protocol selector + host:port, **Token
   (recommended)** or **username/password** auth, and a live
   ⚡ *Test connection* that reports the Splunk version and server name.
3. **Choose indexes** — load the index list from Splunk and check the ones you
   want, or choose auto-discovery for all non-internal indexes, or type names
   manually.

Saving takes effect immediately — no restart. Credentials are only written to
the local `config.json` (gitignored).

> Want to look around first? Click “Skip for now, explore with demo data” in
> step 1 to play with fake data.

## Settings page

The **⚙ Settings** button (top-right) opens the settings modal:

![settings](docs/img/settings.png)

- **Splunk connection**: URL / token or username+password / TLS verification,
  with a test button
- **Tracked indexes**: reload the index list, manual selection,
  auto-discovery mode with include/exclude glob patterns, background rescan
  interval, one-click “track all new”
- **Dashboard behavior**: UI language, cache TTL, default range (days), demo
  mode, listen address & port (restart to apply)

Saves are hot-reloaded (except host/port, which need a restart).

### Index auto-discovery

- **Auto mode**: track every non-internal index the credential can see
  (`_*` always excluded), optionally filtered by include/exclude globs
  (e.g. include `prod_*`, exclude `test_*, summary_*`). The background scanner
  re-runs every N minutes (default 10) and new Splunk indexes are picked up
  automatically.
- **Manual mode**: only the checked indexes are tracked. New indexes are never
  auto-added, but the dashboard shows a 🆕 chip and settings offers
  “track all new” in one click.

The status line shows the last discovery time, visible vs tracked counts and
how many are new.

## Configuration

Three layers, rightmost wins: `built-in defaults` ← `.env` / OS environment ←
`config.json` (saved by the web UI).

| Setting | Env var | Default | Notes |
|---|---|---|---|
| Splunk management URL | `SPLUNK_API_URL` | empty | e.g. `https://splunk.example.com:8089` |
| Bearer token | `SPLUNK_TOKEN` | empty | preferred auth; or username/password |
| Username / password | `SPLUNK_USERNAME` / `SPLUNK_PASSWORD` | empty | basic auth |
| Verify TLS | `SPLUNK_VERIFY_CERTS` | `false` | keep off for self-signed certs |
| Listen address | `DASHBOARD_HOST` | `127.0.0.1` | `0.0.0.0` = LAN (wall display) |
| Port | `DASHBOARD_PORT` | `8091` | restart to apply |
| Cache TTL (s) | `DASHBOARD_CACHE_TTL` | `300` | 30 – 86400 |
| Default range (days) | `DASHBOARD_DEFAULT_DAYS` | `7` | 1 – 365 |
| Demo mode | `DASHBOARD_MOCK` | `0` | `1` = fake data, no Splunk queries |
| Tracked indexes | `TRAFFIC_INDEXES` | empty | JSON array; empty = auto-discovery |
| Include patterns | `TRAFFIC_INDEX_INCLUDE` | empty | JSON array of globs; auto mode |
| Exclude patterns | `TRAFFIC_INDEX_EXCLUDE` | empty | JSON array of globs; `_*` always excluded |
| Index rescan (min) | `INDEX_RESCAN_MINUTES` | `10` | 0 disables new-index detection |

Values coming from `.env` / environment show up as **read-only** fields in the
settings page (with a hint), so the effective configuration is always
predictable. To manage everything in the browser, don't create an `.env` at
all (see `.env.example`).

### API overview

| Endpoint | Description |
|---|---|
| `GET /api/data?from=<epoch>&to=<epoch>` | dashboard dataset (cached) |
| `GET /api/data/stream?...&lang=zh` | SSE variant with live `progress` events |
| `GET /api/config` / `POST /api/config` | read (masked) / save configuration |
| `POST /api/config/test` | test a connection (may carry unsaved credentials) |
| `POST /api/indexes/discover` | list trackable indexes |
| `GET /api/indexes/status` | auto-discovery status (tracked / visible / new) |
| `GET /api/health` | health check |

## Metrics

- **Ingest** = raw bytes from `license_usage` (data entering Splunk;
  recommended for volume-based billing; uncompressed)
- **Disk write** = `per_index_thruput` from metrics (parsed data written to indexes)
- **Disk usage** = `dbinspect sizeOnDisk` (actual compressed usage incl. history)

Values decreasing across the three is expected. History depth is limited by
the `_internal` retention period.

## Security notes

- Credentials live in the local `config.json` — keep the directory's access
  under control (gitignored by default)
- The server binds `127.0.0.1` by default; the settings page has **no login
  auth**, so if you open it to a LAN for a wall display
  (`DASHBOARD_HOST=0.0.0.0`), keep it on a trusted segment or add access
  control via firewall / reverse proxy
- Prefer a dedicated read-only token scoped to the target indexes over an
  admin account

## FAQ

- **401 on test**: invalid or expired token / wrong credentials. Regenerate
  the token or check the account.
- **Cannot connect / timeout**: confirm you're using the **8089 management
  port**, not 8000; check network/firewall between the dashboard host and Splunk.
- **TLS certificate error**: for self-signed certs turn off “Verify TLS
  certificate”, or import the Splunk CA into the system trust store.
- **Empty index list**: the token's role cannot see any index — ask your
  Splunk admin to grant read access to the target indexes.
- **Zeros in the data**: `license_usage` / `per_index_thruput` live in
  `_internal`; history older than its retention cannot be queried.

## License

[MIT](LICENSE)
