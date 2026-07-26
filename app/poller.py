from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

from .config import Settings
from .jdc_api import JdcClient
from .store import Store

log = logging.getLogger("poller")


class Poller:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.latest_events: list[dict[str, Any]] = []
        self.subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        log.info("poller started interval=%ss", self.s.poll_interval)
        while not self._stop.is_set():
            try:
                events = await asyncio.to_thread(self._poll_once)
                self.latest_events = events
                from .config import public_settings

                payload = {
                    "type": "snapshot",
                    "stats": self.store.stats(),
                    "devices": self.store.list_devices(),
                    "events": self.store.list_events(50),
                    "new_events": events,
                    "settings": public_settings(self.s),
                }
                await self._broadcast(payload)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log.error("poll failed: %s\n%s", err, traceback.format_exc())
                self.store.set_meta("last_error", err)
                await self._broadcast({"type": "error", "error": err, "stats": self.store.stats()})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_interval)
            except asyncio.TimeoutError:
                pass
        log.info("poller stopped")

    def _poll_once(self) -> list[dict[str, Any]]:
        client = JdcClient(self.s)
        all_events: list[dict[str, Any]] = []
        try:
            routers = client.list_routers()
            if not routers:
                raise RuntimeError("no routers returned — check WSKEY/tgt")
            self.store.set_meta(
                "routers",
                str([{"mac": r["mac"], "name": r["name"], "status": r.get("status")} for r in routers]),
            )
            ok_any = False
            last_warn = ""
            for r in routers:
                # skip offline routers (status '0')
                if str(r.get("status")) == "0":
                    log.info("skip offline router %s (%s)", r["name"], r["mac"])
                    continue
                try:
                    clients = client.get_client_list(r["feed_id"])
                    # API returns online+offline history rows.
                    # apply_snapshot only needs currently-online set; offline rows
                    # are pushed immediately so UI matches APP (no 2-poll lag).
                    present = [c for c in clients if c.get("online")]
                    offline_now = [c for c in clients if not c.get("online")]
                    ev = self.store.apply_snapshot(
                        router_mac=r["mac"],
                        router_name=r["name"],
                        clients=present,
                        offline_misses=self.s.offline_misses,
                    )
                    ev2 = self.store.mark_offline_clients(
                        router_mac=r["mac"],
                        router_name=r["name"],
                        clients=offline_now,
                    )
                    ev.extend(ev2)
                    all_events.extend(ev)
                    ok_any = True
                    log.info(
                        "router %s (%s): %s online / %s total (api_offline=%s), %s events",
                        r["name"],
                        r["mac"],
                        len(present),
                        len(clients),
                        len(offline_now),
                        len(ev),
                    )
                except Exception as e:
                    last_warn = f"router {r.get('name')}/{r.get('mac')}: {e}"
                    log.warning("router poll fail: %s", last_warn)
            from datetime import datetime

            now = datetime.now().astimezone().isoformat(timespec="seconds")
            self.store.set_meta("last_poll", now)
            if ok_any:
                self.store.set_meta("last_ok", now)
                self.store.set_meta("last_error", "")
            elif last_warn:
                self.store.set_meta("last_error", last_warn)
        finally:
            client.close()
        return all_events

    async def poll_now(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._poll_once)
