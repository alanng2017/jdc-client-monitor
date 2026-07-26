from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")
EVENT_RETENTION_DAYS = 30


def _now_iso() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def _cutoff_iso(days: int = EVENT_RETENTION_DAYS) -> str:
    return (datetime.now(BJ) - timedelta(days=days)).isoformat(timespec="seconds")


def fmt_bj(raw: Any) -> str:
    """Display time as 2026年07月26日 13:12 (Asia/Shanghai, no seconds)."""
    if raw is None:
        return "—"
    s = str(raw).strip()
    if not s or s in ("-", "0", "None", "null"):
        return "—"
    # already formatted — strip trailing :ss if present
    if "年" in s and "月" in s:
        m2 = re.match(
            r"^(\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2})(?::\d{2})?",
            s,
        )
        if m2:
            return m2.group(1)
        return s
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        return f"{m.group(1)}年{m.group(2)}月{m.group(3)}日 {m.group(4)}:{m.group(5)}"
    # try parse iso with offset
    try:
        ss = s
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        d = datetime.fromisoformat(ss)
        if d.tzinfo is None:
            d = d.replace(tzinfo=BJ)
        d = d.astimezone(BJ)
        return d.strftime("%Y年%m月%d日 %H:%M")
    except Exception:
        return s


def _decorate_device(d: dict[str, Any]) -> dict[str, Any]:
    for k in ("first_seen", "last_seen", "last_online", "last_offline", "updated_at"):
        if k in d:
            d[f"{k}_raw"] = d.get(k)
            d[k] = fmt_bj(d.get(k))
    return d


def _decorate_event(d: dict[str, Any]) -> dict[str, Any]:
    d["ts_raw"] = d.get("ts")
    d["ts"] = fmt_bj(d.get("ts"))
    return d


class Store:
    def __init__(self, db_path: str, aliases_file: str):
        self.db_path = db_path
        self.aliases_file = aliases_file
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(aliases_file).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()
        if not Path(aliases_file).exists():
            Path(aliases_file).write_text("{}", encoding="utf-8")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    mac TEXT PRIMARY KEY,
                    router_mac TEXT,
                    router_name TEXT,
                    hostname TEXT,
                    ip TEXT,
                    online INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT,
                    last_seen TEXT,
                    last_online TEXT,
                    last_offline TEXT,
                    miss_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    band TEXT,
                    rssi TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac TEXT NOT NULL,
                    event TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    router_mac TEXT,
                    hostname TEXT,
                    ip TEXT,
                    note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_mac_ts ON events(mac, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT
                );
                """
            )
            # migrate older DBs
            cols = {r[1] for r in c.execute("PRAGMA table_info(devices)").fetchall()}
            if "band" not in cols:
                c.execute("ALTER TABLE devices ADD COLUMN band TEXT")
            if "rssi" not in cols:
                c.execute("ALTER TABLE devices ADD COLUMN rssi TEXT")

    def load_aliases(self) -> dict[str, str]:
        try:
            return json.loads(Path(self.aliases_file).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_aliases(self, aliases: dict[str, str]) -> None:
        Path(self.aliases_file).write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set_alias(self, mac: str, name: str) -> dict[str, str]:
        mac = mac.lower()
        aliases = self.load_aliases()
        name = name.strip()
        if name:
            aliases[mac] = name
        else:
            aliases.pop(mac, None)
        self.save_aliases(aliases)
        return aliases

    def display_name(self, mac: str, hostname: str = "") -> str:
        aliases = self.load_aliases()
        m = mac.lower()
        if m in aliases:
            return aliases[m]
        short = m.replace(":", "")[-6:]
        for k, v in aliases.items():
            if k.replace(":", "")[-6:] == short:
                return v
        return hostname or mac

    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v),
            )

    def get_meta(self, k: str, default: str = "") -> str:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else default

    def purge_old_events(self, days: int = EVENT_RETENTION_DAYS) -> int:
        cutoff = _cutoff_iso(days)
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            return cur.rowcount or 0

    def apply_snapshot(
        self,
        router_mac: str,
        router_name: str,
        clients: list[dict[str, Any]],
        offline_misses: int,
    ) -> list[dict[str, Any]]:
        now = _now_iso()
        seen_macs = {c["mac"].lower() for c in clients}
        new_events: list[dict[str, Any]] = []

        with self._lock, self._conn() as c:
            for cl in clients:
                mac = cl["mac"].lower()
                hostname = cl.get("hostname") or ""
                ip = cl.get("ip") or ""
                band = str(cl.get("band") or "").strip()
                rssi = str(cl.get("rssi") or "").strip()
                row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
                if row is None:
                    c.execute(
                        """
                        INSERT INTO devices(mac,router_mac,router_name,hostname,ip,online,
                          first_seen,last_seen,last_online,last_offline,miss_count,updated_at,band,rssi)
                        VALUES(?,?,?,?,?,1,?,?,?,NULL,0,?,?,?)
                        """,
                        (mac, router_mac, router_name, hostname, ip, now, now, now, now, band or None, rssi or None),
                    )
                    c.execute(
                        "INSERT INTO events(mac,event,ts,router_mac,hostname,ip,note) VALUES(?,?,?,?,?,?,?)",
                        (mac, "online", now, router_mac, hostname, ip, "first_seen"),
                    )
                    new_events.append(
                        {"mac": mac, "event": "online", "ts": now, "hostname": hostname, "ip": ip}
                    )
                else:
                    was_online = bool(row["online"])
                    if not was_online:
                        # 离线→在线：last_online = reonline 时刻
                        c.execute(
                            """
                            UPDATE devices SET router_mac=?, router_name=?,
                              hostname=COALESCE(NULLIF(?,''), hostname),
                              ip=COALESCE(NULLIF(?,''), ip),
                              band=COALESCE(NULLIF(?,''), band),
                              rssi=COALESCE(NULLIF(?,''), rssi),
                              online=1, last_seen=?, last_online=?, miss_count=0, updated_at=?
                            WHERE mac=?
                            """,
                            (router_mac, router_name, hostname, ip, band, rssi, now, now, now, mac),
                        )
                        c.execute(
                            "INSERT INTO events(mac,event,ts,router_mac,hostname,ip,note) VALUES(?,?,?,?,?,?,?)",
                            (
                                mac,
                                "online",
                                now,
                                router_mac,
                                hostname or row["hostname"],
                                ip or row["ip"],
                                "reonline",
                            ),
                        )
                        new_events.append(
                            {
                                "mac": mac,
                                "event": "online",
                                "ts": now,
                                "hostname": hostname or row["hostname"],
                                "ip": ip or row["ip"],
                            }
                        )
                    else:
                        # 已在线：只刷 last_seen/band/rssi，不改 last_online
                        c.execute(
                            """
                            UPDATE devices SET router_mac=?, router_name=?,
                              hostname=COALESCE(NULLIF(?,''), hostname),
                              ip=COALESCE(NULLIF(?,''), ip),
                              band=COALESCE(NULLIF(?,''), band),
                              rssi=COALESCE(NULLIF(?,''), rssi),
                              online=1, last_seen=?, miss_count=0, updated_at=?
                            WHERE mac=?
                            """,
                            (router_mac, router_name, hostname, ip, band, rssi, now, now, mac),
                        )

            rows = c.execute(
                "SELECT * FROM devices WHERE router_mac=? OR router_mac IS NULL OR router_mac=''",
                (router_mac,),
            ).fetchall()
            for row in rows:
                mac = row["mac"]
                if mac in seen_macs:
                    continue
                if row["router_mac"] and row["router_mac"] != router_mac:
                    continue
                if not row["online"] and row["miss_count"] >= offline_misses:
                    continue
                miss = int(row["miss_count"]) + 1
                if miss >= offline_misses and row["online"]:
                    c.execute(
                        """
                        UPDATE devices SET online=0, miss_count=?, last_offline=?, updated_at=?
                        WHERE mac=?
                        """,
                        (miss, now, now, mac),
                    )
                    c.execute(
                        "INSERT INTO events(mac,event,ts,router_mac,hostname,ip,note) VALUES(?,?,?,?,?,?,?)",
                        (mac, "offline", now, router_mac, row["hostname"], row["ip"], f"miss={miss}"),
                    )
                    new_events.append(
                        {
                            "mac": mac,
                            "event": "offline",
                            "ts": now,
                            "hostname": row["hostname"],
                            "ip": row["ip"],
                        }
                    )
                else:
                    c.execute(
                        "UPDATE devices SET miss_count=?, updated_at=? WHERE mac=?",
                        (miss, now, mac),
                    )

        try:
            self.purge_old_events()
        except Exception:
            pass
        return new_events

    def mark_offline_clients(
        self,
        router_mac: str,
        router_name: str,
        clients: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not clients:
            return []
        now = _now_iso()
        new_events: list[dict[str, Any]] = []
        with self._lock, self._conn() as c:
            for cl in clients:
                mac = (cl.get("mac") or "").lower()
                if not mac:
                    continue
                hostname = cl.get("hostname") or ""
                ip = cl.get("ip") or ""
                band = str(cl.get("band") or "").strip()
                rssi = str(cl.get("rssi") or "").strip()
                ts = cl.get("last_offline") or now
                row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
                if row is None:
                    c.execute(
                        """
                        INSERT INTO devices(mac,router_mac,router_name,hostname,ip,online,
                          first_seen,last_seen,last_online,last_offline,miss_count,updated_at,band,rssi)
                        VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?,?)
                        """,
                        (
                            mac,
                            router_mac,
                            router_name,
                            hostname,
                            ip,
                            cl.get("last_online") or ts,
                            cl.get("last_online") or ts,
                            cl.get("last_online") or None,
                            cl.get("last_offline") or ts,
                            99,
                            now,
                            band or None,
                            rssi or None,
                        ),
                    )
                    c.execute(
                        "INSERT INTO events(mac,event,ts,router_mac,hostname,ip,note) VALUES(?,?,?,?,?,?,?)",
                        (mac, "offline", ts, router_mac, hostname, ip, "api_offline"),
                    )
                    new_events.append(
                        {"mac": mac, "event": "offline", "ts": ts, "hostname": hostname, "ip": ip}
                    )
                else:
                    was_online = bool(row["online"])
                    c.execute(
                        """
                        UPDATE devices SET router_mac=?, router_name=?,
                          hostname=COALESCE(NULLIF(?,''), hostname),
                          ip=COALESCE(NULLIF(?,''), ip),
                          band=COALESCE(NULLIF(?,''), band),
                          rssi=COALESCE(NULLIF(?,''), rssi),
                          online=0, miss_count=99,
                          last_offline=COALESCE(NULLIF(?,''), last_offline, ?),
                          updated_at=?
                        WHERE mac=?
                        """,
                        (
                            router_mac,
                            router_name,
                            hostname,
                            ip,
                            band,
                            rssi,
                            cl.get("last_offline") or "",
                            now,
                            now,
                            mac,
                        ),
                    )
                    if was_online:
                        c.execute(
                            "INSERT INTO events(mac,event,ts,router_mac,hostname,ip,note) VALUES(?,?,?,?,?,?,?)",
                            (
                                mac,
                                "offline",
                                ts,
                                router_mac,
                                hostname or row["hostname"],
                                ip or row["ip"],
                                "api_offline",
                            ),
                        )
                        new_events.append(
                            {
                                "mac": mac,
                                "event": "offline",
                                "ts": ts,
                                "hostname": hostname or row["hostname"],
                                "ip": ip or row["ip"],
                            }
                        )
        return new_events

    def get_device(self, mac: str) -> dict[str, Any] | None:
        mac = mac.lower()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["online"] = bool(d["online"])
        aliases = self.load_aliases()
        d["alias"] = aliases.get(d["mac"], "")
        d["display_name"] = self.display_name(d["mac"], d.get("hostname") or "")
        return _decorate_device(d)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM devices ORDER BY online DESC, last_seen DESC"
            ).fetchall()
        aliases = self.load_aliases()
        out = []
        for r in rows:
            d = dict(r)
            d["online"] = bool(d["online"])
            d["alias"] = aliases.get(d["mac"], "")
            d["display_name"] = self.display_name(d["mac"], d.get("hostname") or "")
            out.append(_decorate_device(d))
        return out

    def list_events(
        self,
        limit: int = 200,
        mac: str | None = None,
        days: int = EVENT_RETENTION_DAYS,
    ) -> list[dict[str, Any]]:
        cutoff = _cutoff_iso(days)
        with self._lock, self._conn() as c:
            if mac:
                rows = c.execute(
                    "SELECT * FROM events WHERE mac=? AND ts>=? ORDER BY id DESC LIMIT ?",
                    (mac.lower(), cutoff, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events WHERE ts>=? ORDER BY id DESC LIMIT ?",
                    (cutoff, limit),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["display_name"] = self.display_name(d["mac"], d.get("hostname") or "")
            out.append(_decorate_event(d))
        return out

    def stats(self) -> dict[str, Any]:
        with self._lock, self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM devices").fetchone()["n"]
            online = c.execute("SELECT COUNT(*) n FROM devices WHERE online=1").fetchone()["n"]
            events = c.execute(
                "SELECT COUNT(*) n FROM events WHERE ts>=?",
                (_cutoff_iso(),),
            ).fetchone()["n"]
        last_poll = self.get_meta("last_poll")
        last_ok = self.get_meta("last_ok")
        return {
            "devices_total": total,
            "devices_online": online,
            "devices_offline": total - online,
            "events_total": events,
            "last_poll": fmt_bj(last_poll) if last_poll else "",
            "last_poll_raw": last_poll,
            "last_error": self.get_meta("last_error"),
            "last_ok": fmt_bj(last_ok) if last_ok else "",
            "last_ok_raw": last_ok,
            "event_retention_days": EVENT_RETENTION_DAYS,
        }
