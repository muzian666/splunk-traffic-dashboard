"""Splunk traffic dashboard - standalone WebUI.

Queries Splunk over the 8089 management REST API for daily per-index ingest
statistics and serves an ECharts dashboard:

  - daily event count        | tstats ... by _time span=1d, index
  - daily ingest bytes (GB)  _internal license_usage.log  (billing metric: raw bytes in)
  - daily disk write (GB)    _internal metrics.log per_index_thruput
  - current disk usage (GB)  dbinspect sizeOnDisk

On first run the web UI shows a setup wizard; the connection it saves lives in
config.json next to this package (gitignored) and can be changed any time via
the settings button. See traffic_dashboard/config.py for the config layering.

Results are cached to traffic_dashboard/cache.json (TTL via the settings page /
DASHBOARD_CACHE_TTL, default 300s) so the wall-display page stays instant. If
Splunk is unreachable the last good dataset is served with "stale": true.

Run:  python -m traffic_dashboard.server     (from the project root)
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import RuntimeConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("traffic_dash")

ROOT = Path(__file__).resolve().parent
CONFIG = RuntimeConfig()

CACHE_FILE = ROOT / "cache.json"
GB = 1024.0 ** 3


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the background index auto-discovery scanner with the server."""
    threading.Thread(target=_index_scanner_loop, daemon=True,
                     name="index-scanner").start()
    yield


app = FastAPI(title="Splunk traffic dashboard", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# --------------------------------------------------------------------------- #
# Splunk REST
# --------------------------------------------------------------------------- #
def _splunk_client(overrides: dict | None = None,
                   timeout: float = 60.0) -> tuple[httpx.Client, str]:
    """HTTP client + base URL from the live config, optionally with field
    overrides (used by the connection-test / index-discovery endpoints so the
    setup wizard can try credentials before they are saved)."""
    d = CONFIG.as_dict()
    if overrides:
        for k, v in overrides.items():
            if k in d and v not in (None, ""):
                d[k] = v
    url = str(d["splunk_url"]).rstrip("/")
    if d["splunk_token"]:
        headers = {"Authorization": f"Bearer {d['splunk_token']}"}
        auth = None
    else:
        headers = {}
        auth = (d["splunk_username"], d["splunk_password"])
    client = httpx.Client(timeout=httpx.Timeout(timeout),
                          verify=bool(d["verify_certs"]), auth=auth, headers=headers)
    return client, url


def _check(r: httpx.Response) -> None:
    if r.status_code == 401:
        raise RuntimeError("Splunk 认证失败 (401) - 请检查 Token 或用户名/密码")
    if r.status_code >= 400:
        raise RuntimeError(f"Splunk HTTP {r.status_code}: {r.text[:200]}")


def splunk_search(search: str, earliest: str, latest: str = "now",
                  overrides: dict | None = None) -> list[dict]:
    """Submit a search job, poll it to completion, and return the FINAL results.

    The /export endpoint streams early preview rows (unstable for freshly
    ingested data), so we use the classic jobs flow instead.
    """
    if not search.lstrip().startswith("|"):
        search = f"search {search}"  # REST requires the leading verb for raw SPL
    client, url = _splunk_client(overrides)
    with client:
        r = client.post(f"{url}/services/search/jobs",
                        data={"search": search, "earliest_time": earliest,
                              "latest_time": latest, "output_mode": "json"})
        _check(r)
        sid = r.json().get("sid")
        if not sid:
            raise RuntimeError(f"no sid in response: {r.text[:200]}")

        deadline = time.time() + 300
        while True:
            if time.time() > deadline:
                raise RuntimeError(f"search timed out after 300s (sid={sid})")
            j = client.get(f"{url}/services/search/jobs/{sid}",
                           params={"output_mode": "json"})
            _check(j)
            content = j.json()["entry"][0]["content"]
            state = content.get("dispatchState")
            if state == "FAILED":
                raise RuntimeError(f"search FAILED: {content.get('messages')}")
            if state in ("DONE", "FINALIZE"):
                break
            time.sleep(0.5)

        rows: list[dict] = []
        offset = 0
        while True:
            res = client.get(f"{url}/services/search/jobs/{sid}/results",
                             params={"output_mode": "json", "count": 50000, "offset": offset})
            _check(res)
            batch = res.json().get("results", [])
            rows.extend(batch)
            if len(batch) < 50000:
                return rows
            offset += 50000


# --------------------------------------------------------------------------- #
# Index discovery (for the setup wizard / settings page)
# --------------------------------------------------------------------------- #
_IDX_CACHE: dict[str, Any] = {"ts": 0.0, "names": []}
_IDX_LOCK = threading.Lock()


def discover_indexes(overrides: dict | None = None, force: bool = False) -> list[str]:
    """Non-internal indexes visible to the credential (cached 5 min in memory).

    Tries the REST index listing first, falls back to an eventcount search for
    tokens that may not read /services/data/indexes.
    """
    if not overrides:
        with _IDX_LOCK:
            if not force and _IDX_CACHE["names"] and time.time() - _IDX_CACHE["ts"] < 300:
                return list(_IDX_CACHE["names"])

    names: list[str] | None = None
    err: Exception | None = None
    try:
        client, url = _splunk_client(overrides, timeout=20.0)
        with client:
            r = client.get(f"{url}/services/data/indexes",
                           params={"output_mode": "json", "count": "-1"})
            if r.status_code == 200:
                names = sorted(e.get("name", "") for e in r.json().get("entry", [])
                               if not e.get("name", "").startswith("_"))
            else:
                err = RuntimeError(f"Splunk HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001 - fall through to the search fallback
        err = e

    if not names:
        try:
            rows = splunk_search(
                "| eventcount summarize=false index=* | dedup index | fields index",
                "0", overrides=overrides)
            names = sorted(r["index"] for r in rows
                           if not str(r.get("index", "")).startswith("_"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"索引列表获取失败：{_friendly_conn_error(e)}")

    if not names and err:
        raise RuntimeError(f"索引列表获取失败：{_friendly_conn_error(err)}")
    if not overrides:
        with _IDX_LOCK:
            _IDX_CACHE["ts"], _IDX_CACHE["names"] = time.time(), names
    return names


def effective_indexes() -> list[str]:
    """Tracked indexes: the configured list (manual mode), or the discovered
    indexes filtered by the include/exclude globs (auto-discovery mode).
    Demo set when unconfigured."""
    idx = list(CONFIG.get("indexes") or [])
    if idx:
        return idx
    if CONFIG.credentials_present():
        d = CONFIG.as_dict()
        return filter_indexes(discover_indexes(), d["index_include"], d["index_exclude"])
    return list(DEMO_INDEXES)


def filter_indexes(names: list[str], include: list[str], exclude: list[str]) -> list[str]:
    """Auto-mode glob filter: keep names matching include (empty = all),
    drop names matching exclude. Internal _* never reaches this list."""
    out = []
    for n in names:
        if include and not any(fnmatch(n, p) for p in include):
            continue
        if any(fnmatch(n, p) for p in exclude):
            continue
        out.append(n)
    return out


def _friendly_conn_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "connect" in low or "getaddrinfo" in low or "timed out" in low or "ssl" in low:
        return "Splunk 暂不可达（请检查地址 / 8089 端口 / 防火墙；自签名证书请关闭证书校验）"
    return s[:180]


def index_status() -> dict:
    """Snapshot for the UI: tracked vs available indexes + newly appeared."""
    d = CONFIG.as_dict()
    mode = "manual" if d["indexes"] else "auto"
    tracked = list(d["indexes"])
    if not CONFIG.credentials_present():
        return {"ok": False, "configured": False, "mode": mode, "tracked": tracked,
                "available": [], "new": [], "last_scan_ts": _IDX_CACHE["ts"],
                "rescan_minutes": d["index_rescan_minutes"], "message": "尚未配置 Splunk 连接"}
    try:
        available = discover_indexes()
    except Exception as e:  # noqa: BLE001 - surface as status, not a 500
        return {"ok": False, "configured": True, "mode": mode, "tracked": tracked,
                "available": [], "new": [], "last_scan_ts": _IDX_CACHE["ts"],
                "rescan_minutes": d["index_rescan_minutes"],
                "message": f"自动发现失败：{_friendly_conn_error(e)}"}
    if mode == "auto":
        tracked = filter_indexes(available, d["index_include"], d["index_exclude"])
    seen = set(d.get("indexes_seen") or [])
    new = sorted(n for n in available if seen and n not in seen)
    return {"ok": True, "configured": True, "mode": mode, "tracked": tracked,
            "available": available, "new": new, "last_scan_ts": _IDX_CACHE["ts"],
            "rescan_minutes": d["index_rescan_minutes"]}


def _index_scan_once() -> None:
    """Re-discover indexes and record newly appeared ones (vs the seen set)."""
    names = discover_indexes(force=True)
    seen = set(CONFIG.get("indexes_seen") or [])
    fresh = [n for n in names if n not in seen]
    if seen and fresh:
        log.info("index auto-discovery: new indexes appeared: %s", ", ".join(fresh))
    if fresh or not seen:
        CONFIG.update({"indexes_seen": names})


def _index_scanner_loop() -> None:
    while True:
        minutes = int(CONFIG.get("index_rescan_minutes"))
        try:
            if minutes > 0 and CONFIG.credentials_present():
                _index_scan_once()
        except Exception:
            log.warning("index rescan failed (will retry next cycle)", exc_info=True)
        time.sleep(max(60, minutes * 60) if minutes > 0 else 300)


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _day(epoch: Any) -> str:
    """Export JSON returns _time either as epoch ("1787026771.000") or as a
    formatted local string ("2026-08-16 00:00:00.000 HKT") - handle both."""
    s = str(epoch or "")
    if not s:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(float(s)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _in_list(indexes: list[str]) -> str:
    return ",".join(indexes)


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #
def date_list(earliest: float, latest: float) -> tuple[list[str], bool]:
    """Local dates covered by [earliest, latest]; partial=True if it ends today."""
    d0 = datetime.fromtimestamp(earliest).date()
    d1 = datetime.fromtimestamp(latest).date()
    dates = []
    cur = d0
    while cur <= d1 and len(dates) < 401:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates, d1 == datetime.now().date()


def build_dataset(earliest: float, latest: float) -> dict:
    """Run the Splunk searches and assemble the frontend payload."""
    if CONFIG.mock or not CONFIG.credentials_present():
        log.warning("mock mode (mock=%s, configured=%s)", CONFIG.mock,
                    CONFIG.credentials_present())
        return mock_dataset(earliest, latest)

    indexes = effective_indexes()
    dates, partial = date_list(earliest, latest)
    pos = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    counts = {idx: [0] * n for idx in indexes}
    gbs = {idx: [0.0] * n for idx in indexes}
    disk_write = {idx: [0.0] * n for idx in indexes}
    disk_now = {idx: 0.0 for idx in indexes}
    data_range: dict = {}
    timings: dict[str, int] = {}
    est, lst = str(int(earliest)), str(int(latest))

    t0 = time.time()
    for row in splunk_search(
            f"| tstats count where index IN ({_in_list(indexes)}) by _time span=1d, index",
            est, lst):
        d, idx = _day(row.get("_time")), row.get("index")
        if idx in counts and d in pos:
            counts[idx][pos[d]] = int(_f(row.get("count")))
    timings["events"] = int((time.time() - t0) * 1000)

    t0 = time.time()
    for row in splunk_search(
            f"index=_internal source=*license_usage.log type=Usage idx IN ({_in_list(indexes)}) "
            f"| bin _time span=1d | stats sum(b) as bytes by _time, idx",
            est, lst):
        d, idx = _day(row.get("_time")), row.get("idx")
        if idx in gbs and d in pos:
            gbs[idx][pos[d]] = _f(row.get("bytes")) / GB
    timings["license_bytes"] = int((time.time() - t0) * 1000)

    t0 = time.time()
    for row in splunk_search(
            f"index=_internal source=*metrics.log group=per_index_thruput series IN ({_in_list(indexes)}) "
            f"| bin _time span=1d | stats sum(kb) as kb by _time, series",
            est, lst):
        d, idx = _day(row.get("_time")), row.get("series")
        if idx in disk_write and d in pos:
            disk_write[idx][pos[d]] = _f(row.get("kb")) / 1024 / 1024
    timings["disk_write"] = int((time.time() - t0) * 1000)

    t0 = time.time()
    for row in splunk_search(
            f"| dbinspect index=* | search index IN ({_in_list(indexes)}) "
            f"| eval mb=coalesce(sizeOnDiskMB, if(isnull(sizeOnDisk), 0, sizeOnDisk/1048576)) "
            f"| stats sum(mb) as mb by index",
            "-1h"):
        idx = row.get("index")
        if idx in disk_now:
            disk_now[idx] = _f(row.get("mb")) / 1024
    timings["dbinspect"] = int((time.time() - t0) * 1000)

    # all-time first/last record per index (data coverage, not window-limited)
    t0 = time.time()
    for row in splunk_search(
            f"| tstats min(_time) as ft, max(_time) as lt where index IN ({_in_list(indexes)}) by index",
            "0"):
        idx = row.get("index")
        if idx in indexes:
            ft, lt = _f(row.get("ft")), _f(row.get("lt"))
            data_range[idx] = {
                "first": datetime.fromtimestamp(ft).strftime("%Y-%m-%d") if ft else "-",
                "last": datetime.fromtimestamp(lt).strftime("%Y-%m-%d") if lt else "-",
                "first_ts": int(ft) if ft else 0,
                "last_ts": int(lt) if lt else 0,
            }
    timings["record_range"] = int((time.time() - t0) * 1000)

    return finalize_dataset(dates, counts, gbs, disk_write, disk_now,
                            indexes=indexes, timings=timings, mock=False,
                            partial_last=partial, data_range=data_range)


def finalize_dataset(dates, counts, gbs, disk_write, disk_now, *, indexes,
                     timings, mock, partial_last=False, data_range=None) -> dict:
    n = len(dates)
    # ignore the trailing partial day (today, still in progress) for averages/peaks
    full = max(1, n - 1) if partial_last and n > 1 else n
    order = sorted(indexes, key=lambda i: -sum(gbs.get(i, [0.0])[:full] or [0.0]))
    total_gb = sum(sum(gbs.get(i, [])[:full]) for i in indexes) or 1.0

    summary = []
    for idx in order:
        c, g = counts.get(idx, []), gbs.get(idx, [])
        c_full, g_full = c[:full], g[:full]
        peak = max(g_full) if g_full else 0.0
        peak_date = dates[g_full.index(peak)] if peak > 0 else "-"
        gb_total = sum(g_full)
        summary.append({
            "index": idx,
            "count_total": int(sum(c_full)),
            "gb_total": round(gb_total, 3),
            "gb_daily_avg": round(gb_total / full, 3) if full else 0.0,
            "gb_peak": round(peak, 3),
            "gb_peak_date": peak_date,
            "pct": round(gb_total / total_gb * 100, 2),
            "disk_write_total": round(sum(disk_write.get(idx, [])[:full]), 3),
            "disk_now": round(disk_now.get(idx, 0.0), 2),
        })

    totals_by_day = [round(sum(gbs.get(i, [0.0] * n)[d] for i in indexes), 3)
                     for d in range(n)]
    counts_by_day = [sum(counts.get(i, [0] * n)[d] for i in indexes) for d in range(n)]
    peak_d = max(totals_by_day[:full]) if full else 0.0

    return {
        "mock": mock,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": n - 1,
        "dates": dates,
        "partial_last": partial_last,
        "data_range": data_range or {},
        "indexes": order,
        "counts": counts,
        "gbs": gbs,
        "disk_write": disk_write,
        "disk_now": disk_now,
        "totals_gb_by_day": totals_by_day,
        "totals_count_by_day": counts_by_day,
        "summary": summary,
        "kpi": {
            "count_total": int(sum(counts_by_day[:full])),
            "gb_total": round(sum(totals_by_day[:full]), 2),
            "gb_daily_avg": round(sum(totals_by_day[:full]) / full, 2) if full else 0.0,
            "gb_peak": round(peak_d, 2),
            "gb_peak_date": dates[totals_by_day[:full].index(peak_d)] if peak_d > 0 else "-",
            "disk_now_total": round(sum(disk_now.values()), 2),
        },
        "query_ms": timings,
    }


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        log.exception("cache write failed")


def get_dataset(earliest: float, latest: float, refresh: bool) -> dict:
    cache = load_cache()
    key = f"{CONFIG.fingerprint()}:{int(earliest)}:{int(latest)}"
    entry = cache.get(key)
    if not refresh and entry and time.time() - entry.get("ts", 0) < int(CONFIG.get("cache_ttl")):
        entry["data"]["cached"] = True
        return entry["data"]
    try:
        data = build_dataset(earliest, latest)
    except Exception as e:
        log.exception("Splunk fetch failed")
        if entry:
            log.warning("serving stale cache for key=%s", key)
            entry["data"]["stale"] = True
            return entry["data"]
        raise HTTPException(status_code=502, detail=f"Splunk query failed: {e}")
    cache[key] = {"ts": time.time(), "data": data}
    save_cache(cache)
    return data


# --------------------------------------------------------------------------- #
# Mock data (demo mode when Splunk creds are absent / for UI previews)
# --------------------------------------------------------------------------- #
MOCK_PROFILE = {  # index: (events/day, GB/day)
    "network_firewall": (4_000_000, 6.0),
    "network_proxy": (2_500_000, 3.5),
    "network_vpn": (300_000, 0.6),
    "ids_alerts": (60_000, 0.25),
    "edr_alerts": (25_000, 0.1),
    "antivirus": (12_000, 0.05),
    "email_gateway": (180_000, 0.7),
    "waf_logs": (400_000, 1.1),
    "dns_logs": (900_000, 0.9),
    "cloud_audit": (150_000, 1.2),
    "auth_logs": (70_000, 0.2),
}
DEMO_INDEXES = list(MOCK_PROFILE)


def mock_dataset(earliest: float, latest: float) -> dict:
    indexes = list(CONFIG.get("indexes") or []) or DEMO_INDEXES
    dates, partial = date_list(earliest, latest)
    n = len(dates)
    counts, gbs, disk_write, disk_now = {}, {}, {}, {}
    now = datetime.now()
    for idx in indexes:
        base_c, base_g = MOCK_PROFILE.get(idx, (50_000, 0.3))
        cs, gs, dw = [], [], []
        for d in dates:
            rnd = random.Random(f"{idx}|{d}")
            weekend = datetime.strptime(d, "%Y-%m-%d").weekday() >= 5
            jitter = rnd.uniform(0.75, 1.3) * (0.6 if weekend else 1.0)
            c = int(base_c * jitter)
            g = base_g * jitter * rnd.uniform(0.95, 1.05)
            if d == dates[-1]:  # partial today
                frac = (now.hour * 3600 + now.minute * 60) / 86400
                c, g = int(c * frac), g * frac
            cs.append(c)
            gs.append(round(g, 4))
            dw.append(round(g * rnd.uniform(0.35, 0.5), 4))
        counts[idx], gbs[idx], disk_write[idx] = cs, gs, dw
        disk_now[idx] = round(sum(gs) * 0.42 * 6, 2)  # ~6 windows of history
    return finalize_dataset(dates, counts, gbs, disk_write, disk_now,
                            indexes=indexes, timings={"mock": 0}, mock=True,
                            partial_last=partial,
                            data_range={i: {"first": dates[0], "last": dates[-1],
                                            "first_ts": int(earliest),
                                            "last_ts": int(latest)} for i in indexes})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=FileResponse, include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/data")
def api_data(days: int | None = Query(None, ge=1, le=365),
             from_: float | None = Query(None, alias="from"),
             to_: float | None = Query(None, alias="to"),
             refresh: bool = False) -> dict:
    now = time.time()
    if from_ is None:
        d = days or 30
        start = (datetime.now() - timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        from_, to_ = start.timestamp(), now
    from_ = max(from_, 0)
    to_ = min(to_ if to_ is not None else now, now + 300)
    if to_ - from_ < 60:
        raise HTTPException(status_code=400, detail="time range too small (min 60s)")
    if to_ - from_ > 400 * 86400:
        raise HTTPException(status_code=400, detail="time range too large (max 400 days)")
    return get_dataset(from_, to_, refresh)


# --------------------------------------------------------------------------- #
# Configuration (setup wizard + settings page)
# --------------------------------------------------------------------------- #
_CONN_KEYS = ("splunk_url", "splunk_token", "splunk_username", "splunk_password",
              "verify_certs")


def _conn_overrides(payload: dict | None) -> dict | None:
    """Credential overrides from the request body (for test-before-save).
    Empty strings mean "not provided" -> fall back to the saved config."""
    if not payload:
        return None
    over = {k: payload.get(k) for k in _CONN_KEYS if k in payload}
    if not any(str(v or "").strip() for v in over.values()) and "verify_certs" not in over:
        return None
    return over


@app.get("/api/config")
def api_config() -> dict:
    return CONFIG.masked()


@app.post("/api/config")
def api_config_save(payload: dict | None = None) -> dict:
    before = (CONFIG.get("host"), CONFIG.get("port"))
    try:
        CONFIG.update(payload or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        CACHE_FILE.unlink()
    except FileNotFoundError:
        pass
    with _IDX_LOCK:  # index list may change with the new connection
        _IDX_CACHE["ts"], _IDX_CACHE["names"] = 0.0, []
    after = (CONFIG.get("host"), CONFIG.get("port"))
    return {"ok": True, "restart_required": before != after, "config": CONFIG.masked()}


@app.post("/api/config/test")
def api_config_test(payload: dict | None = None) -> dict:
    over = _conn_overrides(payload)
    d = CONFIG.as_dict()
    if over:
        for k, v in over.items():
            if v not in (None, ""):
                d[k] = v
    url = str(d["splunk_url"]).rstrip("/")
    if not url:
        return {"ok": False, "message": "请先填写 Splunk 地址"}
    if not (d["splunk_token"] or (d["splunk_username"] and d["splunk_password"])):
        return {"ok": False, "message": "请填写 Token 或用户名/密码"}
    try:
        client, base = _splunk_client(over, timeout=10.0)
        with client:
            r = client.get(f"{base}/services/server/info", params={"output_mode": "json"})
        if r.status_code == 401:
            return {"ok": False, "message": "认证失败 (401)：Token 或用户名/密码不正确"}
        if r.status_code >= 400:
            return {"ok": False, "message": f"Splunk HTTP {r.status_code}: {r.text[:200]}"}
        content = r.json()["entry"][0]["content"]
        return {"ok": True, "version": content.get("version", "?"),
                "server_name": content.get("serverName", "?"),
                "message": f"连接成功：Splunk {content.get('version', '?')}（{content.get('serverName', '?')}）"}
    except Exception as e:  # noqa: BLE001 - surface a friendly message to the UI
        msg = str(e)
        if "certificate" in msg.lower() or "ssl" in msg.lower():
            return {"ok": False, "message": "TLS 证书校验失败：自签名证书请关闭“验证 TLS 证书”，或将 CA 导入系统信任库"}
        if "connect" in msg.lower() or "timed out" in msg.lower() or "getaddrinfo" in msg.lower():
            return {"ok": False, "message": "无法连接：请确认地址与 8089 管理端口可达（注意不是网页端口 8000）"}
        return {"ok": False, "message": f"连接失败：{msg[:200]}"}


@app.post("/api/indexes/discover")
def api_indexes_discover(payload: dict | None = None) -> dict:
    over = _conn_overrides(payload)
    if not over and not CONFIG.credentials_present():
        return {"ok": False, "message": "尚未配置 Splunk 连接，请先完成上一步"}
    try:
        names = discover_indexes(overrides=over)
        return {"ok": True, "indexes": names}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300]}


@app.get("/api/indexes/status")
def api_indexes_status() -> dict:
    return index_status()


@app.get("/api/health")
def api_health() -> dict:
    cache = load_cache()
    ages = {k: int(time.time() - v.get("ts", 0)) for k, v in cache.items()}
    d = CONFIG.as_dict()
    return {
        "splunk_url": d["splunk_url"],
        "splunk_configured": CONFIG.credentials_present(),
        "mock": CONFIG.mock or not CONFIG.credentials_present(),
        "indexes": len(d["indexes"]),
        "port": d["port"],
        "cache_age_s": ages,
    }


if __name__ == "__main__":
    log.info("traffic dashboard on http://%s:%s (splunk=%s mock=%s)",
             CONFIG.get("host"), CONFIG.get("port"), CONFIG.splunk_url or "unset",
             CONFIG.mock or not CONFIG.credentials_present())
    uvicorn.run(app, host=CONFIG.get("host"), port=int(CONFIG.get("port")),
                log_level="info")
