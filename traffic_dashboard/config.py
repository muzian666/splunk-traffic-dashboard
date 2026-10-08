"""Runtime configuration for the Splunk traffic dashboard.

Layering (later layers win):
    built-in defaults  <-  .env / OS environment  <-  config.json (web UI)

config.json is written by the first-run setup wizard and the settings page,
and holds the Splunk credential - it is gitignored on purpose. Keys that are
set via .env / OS environment are reported to the UI as "locked" (fields stay
read-only there), so the effective configuration is always predictable.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "config.json"

# config key -> environment variable (documented in .env.example)
ENV_MAP = {
    "splunk_url": "SPLUNK_API_URL",
    "splunk_token": "SPLUNK_TOKEN",
    "splunk_username": "SPLUNK_USERNAME",
    "splunk_password": "SPLUNK_PASSWORD",
    "verify_certs": "SPLUNK_VERIFY_CERTS",
    "host": "DASHBOARD_HOST",
    "port": "DASHBOARD_PORT",
    "cache_ttl": "DASHBOARD_CACHE_TTL",
    "mock": "DASHBOARD_MOCK",
    "default_days": "DASHBOARD_DEFAULT_DAYS",
    "indexes": "TRAFFIC_INDEXES",
    "index_include": "TRAFFIC_INDEX_INCLUDE",
    "index_exclude": "TRAFFIC_INDEX_EXCLUDE",
    "index_rescan_minutes": "INDEX_RESCAN_MINUTES",
}

DEFAULTS: dict[str, Any] = {
    "splunk_url": "",
    "splunk_token": "",
    "splunk_username": "",
    "splunk_password": "",
    "verify_certs": False,
    "host": "127.0.0.1",
    "port": 8091,
    "cache_ttl": 300,
    "mock": False,
    "default_days": 7,  # time range loaded on page open
    "indexes": [],  # [] = auto-discovery mode: track matching non-internal indexes
    "index_include": [],  # glob patterns; empty = all discovered non-internal indexes
    "index_exclude": [],  # glob patterns subtracted after include; _* always excluded
    "index_rescan_minutes": 10,  # background re-discovery cadence; 0 = off
    "indexes_seen": [],  # last known index set (written by the scanner, not by users)
}

TRUTHY = ("1", "true", "yes", "on")
_BOOL_KEYS = ("verify_certs", "mock")
_INT_RANGES = {"port": (1, 65535), "cache_ttl": (30, 86400),
               "index_rescan_minutes": (0, 1440), "default_days": (1, 365)}
_LIST_KEYS = ("indexes", "index_include", "index_exclude", "indexes_seen")
_SECRET_KEYS = ("splunk_token", "splunk_password")


def _coerce(key: str, raw: Any) -> Any:
    """Coerce a raw string (env value) into the typed value for `key`."""
    s = str(raw).strip()
    if key in _BOOL_KEYS:
        return s.lower() in TRUTHY
    if key in _INT_RANGES:
        try:
            return int(s)
        except ValueError:
            return DEFAULTS[key]
    if key in _LIST_KEYS:
        try:
            v = json.loads(s)
            return [str(i) for i in v] if isinstance(v, list) else DEFAULTS[key]
        except Exception:
            return DEFAULTS[key]
    return s


class RuntimeConfig:
    """Thread-safe, hot-reloadable settings store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()  # serializes config.json read-merge-write
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._env_keys: set[str] = set()
        self.reload()

    # -- loading ---------------------------------------------------------------
    def reload(self) -> None:
        merged = dict(DEFAULTS)
        env_keys: set[str] = set()

        env: dict[str, str] = dict(dotenv_values(ENV_FILE) or {})
        for var in ENV_MAP.values():
            if var in os.environ:
                env[var] = os.environ[var]
        for key, var in ENV_MAP.items():
            raw = env.get(var)
            if raw is not None and str(raw).strip() != "":
                merged[key] = _coerce(key, raw)
                env_keys.add(key)

        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for key in DEFAULTS:
                    if key in saved:
                        merged[key] = saved[key]
            except Exception:
                pass  # broken config.json -> defaults + env remain

        with self._lock:
            self._data = merged
            self._env_keys = env_keys

    # -- reads -----------------------------------------------------------------
    def get(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    @property
    def splunk_url(self) -> str:
        return str(self.get("splunk_url")).rstrip("/")

    @property
    def mock(self) -> bool:
        return bool(self.get("mock"))

    def credentials_present(self) -> bool:
        d = self.as_dict()
        return bool(d["splunk_url"] and (
            d["splunk_token"] or (d["splunk_username"] and d["splunk_password"])))

    def fingerprint(self) -> str:
        """Stable id of connection + tracked indexes (salt for cache keys)."""
        d = self.as_dict()
        cred = "token" if d["splunk_token"] else "basic"
        blob = "|".join([d["splunk_url"], cred, ",".join(d["indexes"]),
                         ",".join(d["index_include"]), ",".join(d["index_exclude"])])
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    def masked(self) -> dict:
        """API-safe view: secrets reduced to a hint, plus lock/configured flags."""
        view: dict[str, Any] = {}
        for key, val in self.as_dict().items():
            if key in _SECRET_KEYS:
                view[key] = ("••••" + val[-4:]) if len(str(val)) >= 8 else ("set" if val else "")
                view["has_" + key] = bool(val)
            else:
                view[key] = val
        view["locked"] = sorted(self._env_keys)
        view["configured"] = self.credentials_present()
        view["mock_active"] = self.mock or not self.credentials_present()
        return view

    # -- writes ----------------------------------------------------------------
    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist changes to config.json, then hot-reload.

        Secret fields are tri-state: absent = keep current, None = clear,
        non-empty string = replace. Returns the new effective config.
        """
        clean = self._validate(changes or {})
        with self._io_lock:
            saved: dict[str, Any] = {}
            if CONFIG_FILE.exists():
                try:
                    saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                except Exception:
                    saved = {}
            saved.update(clean)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_FILE)
            self.reload()
        return self.as_dict()

    @staticmethod
    def _validate(changes: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in changes.items():
            if key not in DEFAULTS:
                raise ValueError(f"unknown setting: {key}")
            if key in _SECRET_KEYS and val is None:
                out[key] = ""  # explicit clear
                continue
            if key in _BOOL_KEYS:
                out[key] = bool(val)
            elif key in _INT_RANGES:
                lo, hi = _INT_RANGES[key]
                try:
                    iv = int(val)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be an integer")
                if not lo <= iv <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                out[key] = iv
            elif key in _LIST_KEYS:
                if not isinstance(val, list) or not all(
                        isinstance(i, str) and i.strip() for i in val):
                    raise ValueError(f"{key} must be a list of non-empty strings")
                seen: set = set()
                uniq: list[str] = []
                for i in val:
                    i = i.strip()
                    if i and i not in seen:
                        seen.add(i)
                        uniq.append(i)
                out[key] = uniq
            else:
                s = str(val or "").strip()
                if key == "splunk_url":
                    if s and not s.startswith(("http://", "https://")):
                        raise ValueError("splunk_url must start with http:// or https://")
                    s = s.rstrip("/")
                out[key] = s
        return out
