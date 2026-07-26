from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .config import Settings, _norm_mac


def _day_of_year() -> int:
    today = date.today()
    return (today - date(today.year, 1, 1)).days + 1


def make_authorization(body: str, settings: Settings) -> str:
    # mirror JDRouterPush Android deviceKey
    total_days = _day_of_year()
    device_key = hashlib.md5(f"Android{settings.app_version}MI 69:{total_days}".encode()).hexdigest()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    text = f"{device_key}postjson_body{body}{ts}{settings.access_key}{device_key}"
    digest = hmac.new(settings.hmac_key.encode(), text.encode(), hashlib.sha1).digest()
    sig = base64.b64encode(digest).decode()
    return f"smart {settings.access_key}:::{sig}:::{ts}"


class JdcClient:
    def __init__(self, settings: Settings):
        self.s = settings
        if not settings.wskey.strip():
            raise RuntimeError("WSKEY/tgt empty — 网页设置粘贴 tgt，或设 env WSKEY")
        proxy = None
        if settings.https_proxy or settings.http_proxy:
            proxy = settings.https_proxy or settings.http_proxy
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            proxy=proxy,
            headers={
                "User-Agent": "Android",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def _service_headers(self, body: str) -> dict[str, str]:
        return {
            "Authorization": make_authorization(body, self.s),
            "accesskey": self.s.access_key,
            "tgt": self.s.wskey,
            "appkey": "996",
            "User-Agent": "Android",
            "Host": "gw.smart.jd.com",
            "Content-Type": "application/json",
        }

    def _service_params(self) -> dict[str, Any]:
        return {
            "hard_platform": "MI 6",
            "app_version": self.s.app_version,
            "plat_version": 9,
            "channel": "jdCloud",
            "plat": "Android",
        }

    def list_routers(self) -> list[dict[str, Any]]:
        body = ""
        url = self.s.service_base + "listAllUserDevices"
        r = self.client.post(
            url,
            params=self._service_params(),
            headers=self._service_headers(body),
            content=body,
        )
        r.raise_for_status()
        data = r.json()
        if str(data.get("status", data.get("code", ""))) not in ("0", "200", ""):
            if data.get("error"):
                raise RuntimeError(f"listAllUserDevices error: {data.get('error')}")
        result = data.get("result") or []
        routers: list[dict[str, Any]] = []
        groups = result if isinstance(result, list) else [result]
        for g in groups:
            if not isinstance(g, dict):
                continue
            for item in g.get("list") or []:
                mac = _norm_mac(str(item.get("device_id") or item.get("mac") or ""))
                if not mac:
                    continue
                cname = str(item.get("cname") or "")
                # only keep 路由器 (skip AC, etc.)
                if cname and cname != "路由器":
                    continue
                routers.append(
                    {
                        "mac": mac,
                        "name": item.get("device_name") or mac,
                        "feed_id": str(item.get("feed_id") or ""),
                        "status": str(item.get("status") or ""),
                        "raw": item,
                    }
                )
        filt = self.s.router_set()
        if filt:
            tail = {m.replace(":", "")[-6:] for m in filt}
            routers = [
                x
                for x in routers
                if x["mac"] in filt or x["mac"].replace(":", "")[-6:] in tail
            ]
        return routers

    def control_device(self, feed_id: str, cmd: str) -> Any:
        body_obj = {
            "feed_id": feed_id,
            "command": [{"stream_id": "SetParams", "current_value": {"cmd": cmd}}],
        }
        body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
        url = self.s.service_base + "controlDevice"
        r = self.client.post(
            url,
            params=self._service_params(),
            headers=self._service_headers(body),
            content=body,
        )
        if r.status_code != 200:
            body2_obj = {
                "feed_id": feed_id,
                "command": [
                    {
                        "stream_id": "SetParams",
                        "current_value": json.dumps({"cmd": cmd}, ensure_ascii=False),
                    }
                ],
            }
            body2 = json.dumps(body2_obj, ensure_ascii=False, separators=(",", ":"))
            r = self.client.post(
                url,
                params=self._service_params(),
                headers=self._service_headers(body2),
                content=body2,
            )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"controlDevice {cmd} error: {data['error']}")
        result = data.get("result")
        if result is None:
            return None
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                return result
        return result

    def get_client_list(self, feed_id: str) -> list[dict[str, Any]]:
        result = self.control_device(feed_id, "get_device_list")
        clients = _extract_clients(result)
        out: list[dict[str, Any]] = []
        for c in clients:
            mac = _norm_mac(
                str(
                    c.get("mac")
                    or c.get("device_mac")
                    or c.get("macaddr")
                    or c.get("client_mac")
                    or ""
                )
            )
            if not mac:
                continue
            name = (
                c.get("hostname")
                or c.get("name")
                or c.get("device_name")
                or c.get("host_name")
                or c.get("nickname")
                or ""
            )
            ip = c.get("ip") or c.get("ip_addr") or c.get("ipaddr") or c.get("client_ip") or ""
            online = _as_online(c)
            out.append(
                {
                    "mac": mac,
                    "hostname": str(name or ""),
                    "ip": str(ip or ""),
                    "online": online,
                    "band": str(c.get("band") or ""),
                    "rssi": str(c.get("rssi") or ""),
                    "last_online": str(c.get("last_online") or ""),
                    "last_offline": str(c.get("last_offline") or ""),
                    "raw": c,
                }
            )
        watch = self.s.watch_set()
        if watch:
            keys = set()
            for w in watch:
                keys.add(w)
                keys.add(w.replace(":", "")[-6:])
            filtered = []
            for c in out:
                m = c["mac"]
                if m in keys or m.replace(":", "")[-6:] in keys:
                    filtered.append(c)
            out = filtered
        return out


def _as_online(c: dict[str, Any]) -> bool:
    for k in ("online", "is_online", "onLine", "status", "state", "active"):
        if k not in c:
            continue
        v = c[k]
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        s = str(v).lower()
        if s in ("1", "true", "online", "on", "connected", "active", "yes"):
            return True
        if s in ("0", "false", "offline", "off", "disconnected", "inactive", "no"):
            return False
    return True


def _parse_jdc_time(s: str) -> datetime | None:
    """Parse JDC time strings like '2026-07-26 12:03' (Asia/Shanghai, no seconds)."""
    s = (s or "").strip()
    if not s or s in ("0", "None", "null"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            # naive local China time from router
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _row_to_client(row: list | tuple) -> dict[str, Any] | None:
    """Parse JDC array row:
    [mac, ?, hostname, band, last_online, last_offline, online_flag, rssi, model, ...]

    实测：index6 online_flag 对历史设备常恒为 '1'，不可信。
    APP 在线判定 ≈ last_offline 为空/0，或 last_online > last_offline。
    """
    if not row:
        return None
    mac_raw = str(row[0] or "").strip()
    if not mac_raw:
        return None
    hostname = str(row[2] if len(row) > 2 else "") or ""
    band = str(row[3] if len(row) > 3 else "") or ""
    last_online_raw = str(row[4] if len(row) > 4 else "") or ""
    last_offline_raw = str(row[5] if len(row) > 5 else "") or ""
    online_flag = str(row[6] if len(row) > 6 else "")
    rssi = str(row[7] if len(row) > 7 else "") or ""
    model = str(row[8] if len(row) > 8 else "") or ""

    last_online = last_online_raw if last_online_raw not in ("0", "") else ""
    last_offline = last_offline_raw if last_offline_raw not in ("0", "") else ""

    online = _jdc_row_is_online(last_online, last_offline, online_flag)

    return {
        "mac": mac_raw,
        "hostname": hostname or model,
        "name": hostname or model,
        "band": band,
        "last_online": last_online,
        "last_offline": last_offline,
        "online": online,
        "rssi": rssi if rssi not in ("0",) else "",
        "model": model,
        "ip": "",  # API array form has no IP
        "online_flag_raw": online_flag,
    }


def _jdc_row_is_online(last_online: str, last_offline: str, online_flag: str) -> bool:
    """True if device currently connected.

    Priority:
    1) compare last_online vs last_offline timestamps (APP-like)
    2) fallback online_flag only when offline time empty
    """
    t_on = _parse_jdc_time(last_online)
    t_off = _parse_jdc_time(last_offline)

    if t_on and t_off:
        # offline stamp newer → offline (even if flag says 1)
        if t_off >= t_on:
            return False
        return True
    if t_on and not t_off:
        return True
    if t_off and not t_on:
        return False

    # no usable times — fall back to flag
    if online_flag in ("1", "true", "True", "online"):
        return True
    if online_flag in ("0", "false", "False", "offline"):
        return False
    # unknown: treat as offline so we don't inflate online count
    return False


def _extract_clients(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        if "streams" in result:
            try:
                streams = result["streams"]
                # prefer GetParams stream
                cv = None
                for st in streams:
                    if st.get("stream_id") == "GetParams":
                        cv = st.get("current_value")
                        break
                if cv is None and streams:
                    cv = streams[0].get("current_value")
                if isinstance(cv, str):
                    cv = json.loads(cv)
                data = cv.get("data", cv) if isinstance(cv, dict) else cv
                return _extract_clients(data)
            except Exception:
                pass
        for key in ("list", "device_list", "devices", "clients", "data", "client_list", "wifi_list"):
            if key in result:
                return _extract_clients(result[key])
        if any(k in result for k in ("mac", "device_mac", "macaddr")):
            return [result]
        return []
    if isinstance(result, list):
        if not result:
            return []
        # array-of-arrays form from get_device_list
        if isinstance(result[0], (list, tuple)):
            out: list[dict[str, Any]] = []
            for row in result:
                if isinstance(row, (list, tuple)):
                    c = _row_to_client(row)
                    if c:
                        out.append(c)
            return out
        if isinstance(result[0], dict):
            return result  # type: ignore[return-value]
        return []
    if isinstance(result, str):
        try:
            return _extract_clients(json.loads(result))
        except json.JSONDecodeError:
            return []
    return []
