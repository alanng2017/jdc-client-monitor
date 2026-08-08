from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    AuthMiddleware,
    check_password,
    clear_auth_cookie,
    password_set,
    set_auth_cookie,
)
from .config import get_settings, public_settings, save_secrets
from .poller import Poller
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

settings = get_settings()
store = Store(settings.db_path, settings.aliases_file)
poller = Poller(settings, store)

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.wskey.strip():
        log.warning("WSKEY empty — open web UI → 设置 填写；或设 env WSKEY")
    else:
        log.info("WSKEY loaded (masked=%s)", settings.wskey_masked())
    if password_set(settings):
        log.info("UI password auth ENABLED")
    else:
        log.warning("UI password empty — first visit must /setup")
    poller.start()
    yield
    await poller.stop()


app = FastAPI(title="JDC Client Monitor", lifespan=lifespan)
app.add_middleware(AuthMiddleware, settings=settings)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "setup_required": not password_set(settings)}


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, err: str = ""):
    if password_set(settings):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "setup.html",
        {"request": request, "err": err},
    )


@app.post("/setup")
async def setup_post(
    password: str = Form(""),
    password2: str = Form(""),
):
    if password_set(settings):
        return RedirectResponse("/login", status_code=303)

    pw = (password or "").strip()
    pw2 = (password2 or "").strip()
    if len(pw) < 4:
        return RedirectResponse("/setup?err=short", status_code=303)
    if pw != pw2:
        return RedirectResponse("/setup?err=mismatch", status_code=303)

    save_secrets(settings, {"ui_password": pw})
    log.info("first-run UI password set")
    resp = RedirectResponse("/", status_code=303)
    set_auth_cookie(resp, settings)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", err: str = ""):
    if not password_set(settings):
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next or "/", "err": err},
    )


@app.post("/login")
async def login_post(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    if not password_set(settings):
        return RedirectResponse("/setup", status_code=303)
    if not check_password(settings, password):
        n = quote(next or "/", safe="")
        return RedirectResponse(url=f"/login?next={n}&err=1", status_code=303)
    dest = next if next.startswith("/") else "/"
    resp = RedirectResponse(dest, status_code=303)
    set_auth_cookie(resp, settings)
    return resp


@app.post("/logout")
async def logout():
    if not password_set(settings):
        resp = RedirectResponse("/setup", status_code=303)
    else:
        resp = RedirectResponse("/login", status_code=303)
    clear_auth_cookie(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": store.stats(),
            "devices": store.list_devices(),
            "events": store.list_events(100),
            "poll_interval": settings.poll_interval,
            "wskey_set": settings.wskey_configured(),
            "settings_public": public_settings(settings),
            "auth_enabled": password_set(settings),
        },
    )


@app.get("/api/devices")
async def api_devices():
    return {"devices": store.list_devices(), "stats": store.stats()}


@app.get("/api/device/{mac}")
async def api_device_detail(mac: str, limit: int = Query(500, ge=1, le=2000)):
    dev = store.get_device(mac)
    if not dev:
        return JSONResponse({"ok": False, "error": "device not found"}, status_code=404)
    events = store.list_events(limit=limit, mac=mac)
    return {
        "ok": True,
        "device": dev,
        "events": events,
        "event_retention_days": 30,
    }

@app.get("/api/search")
async def api_search_devices(query: str = Query(..., min_length=1)):
    """按自定义名 / 主机名搜索，返回所有同名设备 + 各自上下线事件（合并展示）。"""
    if not query:
        return {"devices": [], "events": []}
    q = query.lower()
    devices = store.list_devices()
    matches = [d for d in devices
               if q in (d.get("display_name") or "").lower()
               or q in (d.get("hostname") or "").lower()
               or q in (d.get("alias") or "").lower()
               or q in (d.get("mac") or "").lower()]
    # 同一 display_name 的才算"同一个备注名"，按备注名分组取设备列表
    from collections import OrderedDict
    groups: dict[str, list] = OrderedDict()
    for d in matches:
        key = d.get("display_name") or d.get("mac")
        groups.setdefault(key, []).append(d)
    result_devices = []
    all_events = []
    for name, devs in groups.items():
        if name == query or query in name:
            result_devices.append({"name": name, "devices": devs, "count": len(devs)})
            for d in devs:
                events = store.list_events(limit=500, mac=d.get("mac"))
                for e in events:
                    e["display_name"] = name
                    e["group_name"] = name
                all_events.extend(events)
    all_events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return {"groups": result_devices, "events": all_events[:500]}


@app.get("/api/events")
async def api_events(limit: int = Query(200, ge=1, le=2000), mac: str | None = None):
    return {"events": store.list_events(limit=limit, mac=mac)}


@app.get("/api/stats")
async def api_stats():
    return store.stats()


@app.get("/api/aliases")
async def api_aliases():
    return store.load_aliases()


@app.post("/api/alias")
async def api_set_alias(mac: str = Form(...), name: str = Form("")):
    aliases = store.set_alias(mac, name)
    return {"ok": True, "aliases": aliases, "devices": store.list_devices()}


@app.get("/api/settings")
async def api_get_settings():
    return public_settings(settings)


@app.post("/api/settings")
async def api_set_settings(
    wskey: str = Form(""),
    clear_wskey: str = Form("0"),
    poll_interval: str = Form(""),
    offline_misses: str = Form(""),
    watch_macs: str = Form(""),
    router_macs: str = Form(""),
    http_proxy: str = Form(""),
    https_proxy: str = Form(""),
    poll_now: str = Form("1"),
    ui_password: str = Form(""),
    clear_ui_password: str = Form("0"),
):
    """网页保存配置。wskey 留空=不改；clear_wskey=1 清空。"""
    updates: dict = {}
    if clear_wskey in ("1", "true", "yes", "on"):
        updates["wskey"] = ""
    elif wskey.strip():
        updates["wskey"] = wskey.strip()

    if poll_interval.strip():
        updates["poll_interval"] = poll_interval.strip()
    if offline_misses.strip():
        updates["offline_misses"] = offline_misses.strip()

    updates["watch_macs"] = watch_macs
    updates["router_macs"] = router_macs
    updates["http_proxy"] = http_proxy
    updates["https_proxy"] = https_proxy

    # clear password → back to setup gate (dangerous but explicit)
    if clear_ui_password in ("1", "true", "yes", "on"):
        updates["ui_password"] = ""
    elif ui_password.strip():
        updates["ui_password"] = ui_password.strip()

    pub = save_secrets(settings, updates)
    log.info(
        "settings saved via UI: wskey_set=%s poll_interval=%s auth=%s",
        pub["wskey_set"],
        pub["poll_interval"],
        pub["ui_password_set"],
    )

    result = {"ok": True, "settings": pub, "stats": store.stats()}
    if poll_now in ("1", "true", "yes", "on") and settings.wskey_configured():
        try:
            events = await poller.poll_now()
            result["polled"] = True
            result["new_events"] = events
            result["devices"] = store.list_devices()
            result["events"] = store.list_events(100)
            result["stats"] = store.stats()
        except Exception as e:
            result["polled"] = False
            result["poll_error"] = str(e)
            result["stats"] = store.stats()
    # if password cleared, client should go setup
    if not password_set(settings):
        result["setup_required"] = True
    return result


@app.post("/api/poll")
async def api_poll():
    try:
        events = await poller.poll_now()
        return {
            "ok": True,
            "new_events": events,
            "devices": store.list_devices(),
            "stats": store.stats(),
            "events": store.list_events(100),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "stats": store.stats()}, status_code=500)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not password_set(settings):
        await ws.close(code=4401)
        return
    token = ws.cookies.get("jdc_auth", "")
    from .auth import make_session_token
    import secrets as _sec

    if not token or not _sec.compare_digest(token, make_session_token(settings)):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = poller.subscribe()
    try:
        await ws.send_json(
            {
                "type": "snapshot",
                "stats": store.stats(),
                "devices": store.list_devices(),
                "events": store.list_events(50),
                "new_events": [],
                "settings": public_settings(settings),
            }
        )
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                await ws.send_json(msg)
            except asyncio.TimeoutError:
                await ws.send_json(
                    {
                        "type": "ping",
                        "stats": store.stats(),
                        "settings": public_settings(settings),
                    }
                )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("ws closed: %s", e)
    finally:
        poller.unsubscribe(q)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
