from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import quote

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse, JSONResponse

from .config import Settings

COOKIE_NAME = "jdc_auth"
# always open (no auth / setup gate)
OPEN_ALWAYS = {"/healthz"}
# first-run setup (only when password not set)
OPEN_SETUP = {"/setup"}
# login/logout when password already set
OPEN_LOGIN = {"/login", "/logout"}


def password_set(settings: Settings) -> bool:
    return bool((settings.ui_password or "").strip())


def _cookie_secret(settings: Settings) -> bytes:
    # bind cookie to password so changing password invalidates sessions
    base = (settings.session_secret or "jdc-client-monitor") + "|" + (settings.ui_password or "")
    return hashlib.sha256(base.encode("utf-8")).digest()


def make_session_token(settings: Settings) -> str:
    if not password_set(settings):
        return ""
    return hmac.new(
        _cookie_secret(settings),
        b"session-v1",
        hashlib.sha256,
    ).hexdigest()


def check_password(settings: Settings, password: str) -> bool:
    expected = (settings.ui_password or "").strip()
    if not expected:
        return False
    return secrets.compare_digest(password or "", expected)


def is_authenticated(request: Request, settings: Settings) -> bool:
    if not password_set(settings):
        return False
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return False
    return secrets.compare_digest(token, make_session_token(settings))


def set_auth_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(settings),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        static = path.startswith("/static/")

        # health always open
        if path in OPEN_ALWAYS:
            return await call_next(request)
        if static:
            return await call_next(request)

        # first run: password not set → only /setup
        if not password_set(self.settings):
            if path in OPEN_SETUP:
                return await call_next(request)
            if path.startswith("/api/") or path == "/ws":
                return JSONResponse(
                    {"ok": False, "error": "setup_required", "setup": True},
                    status_code=401,
                )
            return RedirectResponse(url="/setup", status_code=302)

        # password set: setup page not needed
        if path in OPEN_SETUP:
            return RedirectResponse(url="/login", status_code=302)

        if path in OPEN_LOGIN:
            return await call_next(request)

        if is_authenticated(request, self.settings):
            return await call_next(request)

        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        nxt = path if path.startswith("/") else "/"
        return RedirectResponse(url=f"/login?next={quote(nxt, safe='/?&=')}", status_code=302)
