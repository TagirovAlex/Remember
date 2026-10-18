import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers import api, web
from app.services.notify import fmt_money

logger = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    404: "Страница не найдена",
    403: "Доступ запрещён",
    500: "Внутренняя ошибка сервера. Попробуйте повторить позже",
}


def _render_error(request: Request, code: int, message: str) -> HTMLResponse:
    """Render the error page; fall back to a bare HTML page on template issues."""
    try:
        return web.templates.TemplateResponse(
            request,
            "error.html",
            {"code": code, "message": message},
            status_code=code,
        )
    except Exception:
        logger.exception("Failed to render error page %s", code)
        return HTMLResponse(
            f"<h1>{code}</h1><p>{message}</p>",
            status_code=code,
        )


def create_app() -> FastAPI:
    """Build the FastAPI application: Jinja2 web UI plus a JSON API."""
    web.templates.env.filters["money"] = fmt_money
    app = FastAPI(title="Remember")
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        code = exc.status_code
        if request.url.path.startswith("/api/"):
            detail = exc.detail if isinstance(exc.detail, str) else "Error"
            return JSONResponse({"detail": detail}, status_code=code)
        location = next(
            (v for k, v in (exc.headers or {}).items() if k.lower() == "location"),
            None,
        )
        if location:
            return RedirectResponse(location, status_code=303)
        if code == 404:
            return _render_error(request, 404, _ERROR_MESSAGES[404])
        if code == 401:
            return RedirectResponse("/login", status_code=303)
        if code == 403:
            return _render_error(request, 403, _ERROR_MESSAGES[403])
        message = exc.detail if isinstance(exc.detail, str) else "Произошла ошибка"
        return _render_error(request, code, message)

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return _render_error(request, 500, _ERROR_MESSAGES[500])

    app.include_router(web.router)
    app.include_router(api.router)
    return app