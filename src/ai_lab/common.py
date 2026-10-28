import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind

from ai_lab.telemetry import PROPAGATOR, mark_error


def make_app(title, settings, telemetry, routes):
    @asynccontextmanager
    async def lifespan(app):
        yield
        if hasattr(app.state, "http_client"):
            await app.state.http_client.aclose()
        telemetry.shutdown()

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.telemetry = telemetry

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        # Unknown paths and methods never become telemetry attributes or span names.
        route = request.url.path if request.url.path in routes else "/unmatched"
        method = request.method if request.method in {"GET", "POST"} else "OTHER"
        started = time.monotonic()
        with telemetry.span(
            f"{method} {route}",
            kind=SpanKind.SERVER,
            context=PROPAGATOR.extract(dict(request.headers)),
            attributes={"http.request.method": method, "http.route": route},
        ) as span:
            try:
                response = await call_next(request)
            except Exception:
                mark_error(span, "internal_error")
                response = JSONResponse({"detail": "Internal error"}, status_code=500)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                mark_error(span, "server_error")
            telemetry.record("http", "ok" if response.status_code < 400 else "error", started)
            return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "model_mode": "deterministic-mock"}

    return app


def require_auth(request: Request, settings):
    supplied = request.headers.get("x-lab-key", "")
    if not secrets.compare_digest(supplied.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_fault_permission(scenario, settings):
    if scenario != "healthy" and not settings.allow_faults:
        raise HTTPException(status_code=403, detail="Fault injection disabled")
