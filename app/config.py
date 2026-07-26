from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    wskey: str = ""
    poll_interval: int = 45
    offline_misses: int = 2
    watch_macs: str = ""
    router_macs: str = ""
    port: int = 8780
    db_path: str = "/data/monitor.db"
    aliases_file: str = "/data/aliases.json"
    secrets_file: str = "/data/secrets.json"
    http_proxy: str = ""
    https_proxy: str = ""
    host: str = "0.0.0.0"

    # web UI password (empty = no auth)
    ui_password: str = ""
    session_secret: str = "jdc-client-monitor-session"

    # public constants from JD APP
    access_key: str = "b8f9c108c190a39760e1b4e373208af5cd75feb4"
    hmac_key: str = "706390cef611241d57573ca601eb3c061e174948"
    app_version: str = "6.5.5"
    service_base: str = "https://gw.smart.jd.com/f/service/"
    cloud_base: str = "https://router-app-api.jdcloud.com/v1/regions/cn-north-1/"

    def watch_set(self) -> set[str]:
        return {_norm_mac(x) for x in self.watch_macs.split(",") if x.strip()}

    def router_set(self) -> set[str]:
        return {_norm_mac(x) for x in self.router_macs.split(",") if x.strip()}

    def proxies(self) -> dict | None:
        p = {}
        if self.http_proxy:
            p["http://"] = self.http_proxy
        if self.https_proxy:
            p["https://"] = self.https_proxy
        elif self.http_proxy:
            p["https://"] = self.http_proxy
        return p or None

    def wskey_configured(self) -> bool:
        return bool(self.wskey.strip())

    def wskey_masked(self) -> str:
        k = self.wskey.strip()
        if not k:
            return ""
        if len(k) <= 10:
            return "***"
        return k[:6] + "…" + k[-4:]


def _norm_mac(s: str) -> str:
    s = s.strip().lower().replace("-", ":").replace(".", "")
    if ":" not in s and len(s) == 12:
        s = ":".join(s[i : i + 2] for i in range(0, 12, 2))
    return s


# fields that can be edited from web UI and persisted to secrets file
PERSIST_KEYS = (
    "wskey",
    "poll_interval",
    "offline_misses",
    "watch_macs",
    "router_macs",
    "http_proxy",
    "https_proxy",
    "ui_password",
)


def secrets_path(settings: Settings) -> Path:
    return Path(settings.secrets_file)


def load_secrets(settings: Settings) -> dict:
    p = secrets_path(settings)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_secrets(settings: Settings) -> Settings:
    """Merge secrets file into settings. File wins over env for non-empty values.
    For ui_password / wskey: file value (including empty) is applied if key exists.
    """
    data = load_secrets(settings)
    for k in PERSIST_KEYS:
        if k not in data:
            continue
        v = data[k]
        if v is None:
            continue
        if k in ("poll_interval", "offline_misses"):
            try:
                setattr(settings, k, int(v))
            except (TypeError, ValueError):
                continue
        else:
            setattr(settings, k, str(v))
    return settings


def save_secrets(settings: Settings, updates: dict) -> dict:
    """Update secrets file and live settings. Returns public view."""
    data = load_secrets(settings)
    for k, v in updates.items():
        if k not in PERSIST_KEYS:
            continue
        if k in ("poll_interval", "offline_misses"):
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if k == "poll_interval":
                iv = max(15, min(iv, 86400))
            if k == "offline_misses":
                iv = max(1, min(iv, 20))
            data[k] = iv
            setattr(settings, k, iv)
        else:
            data[k] = str(v).strip() if v is not None else ""
            setattr(settings, k, data[k])
    p = secrets_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return public_settings(settings)


def public_settings(settings: Settings) -> dict:
    return {
        "wskey_set": settings.wskey_configured(),
        "wskey_masked": settings.wskey_masked(),
        "poll_interval": settings.poll_interval,
        "offline_misses": settings.offline_misses,
        "watch_macs": settings.watch_macs,
        "router_macs": settings.router_macs,
        "http_proxy": settings.http_proxy,
        "https_proxy": settings.https_proxy,
        "secrets_file": settings.secrets_file,
        "ui_password_set": bool((settings.ui_password or "").strip()),
    }


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    apply_secrets(s)
    return s
