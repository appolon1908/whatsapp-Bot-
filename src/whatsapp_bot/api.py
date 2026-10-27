"""HTTP application; startup never provisions or migrates databases."""

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, TokenVerifier
from .config import Settings
from .db import assert_runtime_role, make_engine
from .errors import CommerceError
from .messaging import IngressService
from .models import Base
from .routes import admin, catalog, conversations, orders

logger = logging.getLogger("whatsapp_bot.api")


class BodyLimitMiddleware:
    """Bound streamed bodies before parsing, including chunked requests."""

    def __init__(self, app, limit=1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                response = JSONResponse(
                    {"error": {"code": "payload_too_large", "message": "Body exceeds 1 MiB"}}, status_code=413
                )
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, engine=None, token_verifier=None):
    settings = settings or Settings()
    owned_engine = engine is None
    if engine is None and settings.database_url.get_secret_value():
        engine = make_engine(settings.database_url.get_secret_value())
    verifier = token_verifier or TokenVerifier(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned_engine and engine is not None:
            engine.dispose()

    app = FastAPI(title="Codestra WhatsApp Commerce", version="0.1.0", lifespan=lifespan)
    app.state.engine, app.state.verifier, app.state.settings = engine, verifier, settings
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def response_metadata(request, call_next):
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(uuid4())
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def public_error(request, error):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=error.status,
            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else {},
        )

    app.add_exception_handler(CommerceError, public_error)
    app.add_exception_handler(AuthError, public_error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        fields = [{"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "Request validation failed", "fields": fields}},
            status_code=422,
        )

    @app.exception_handler(IntegrityError)
    async def conflict(request, error):
        return JSONResponse(
            {"error": {"code": "resource_conflict", "message": "Request conflicts with existing data"}},
            status_code=409,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        logger.error("database_request_failed", extra={"error_type": type(error).__name__})
        return JSONResponse(
            {
                "error": {
                    "code": "database_unavailable",
                    "message": "Commerce storage is temporarily unavailable",
                }
            },
            status_code=503,
        )

    @app.get("/healthz", tags=["operations"])
    def health():
        return {"status": "ok", "service": "codestra-whatsapp-commerce"}

    @app.get("/readyz", tags=["operations"])
    def ready():
        checks = {
            "database": False,
            "runtime_role": False,
            "schema": False,
            "identity": verifier.is_configured(),
            "ingress": len(settings.ingress_secret.get_secret_value().encode()) >= 32,
        }
        if engine is not None:
            try:
                assert_runtime_role(engine)
                checks["database"] = checks["runtime_role"] = True
                tables = set(inspect(engine).get_table_names(schema="commerce"))
                checks["schema"] = {table.name for table in Base.metadata.tables.values()} <= tables
            except (RuntimeError, SQLAlchemyError, ValueError):
                pass
        available = all(checks.values())
        return JSONResponse(
            {
                "status": "ready" if available else "not_ready",
                "checks": checks,
                "outbound_enabled": settings.outbound_enabled,
            },
            status_code=200 if available else 503,
        )

    @app.post("/internal/v1/whatsapp/events", status_code=202, tags=["internal ingress"])
    async def ingest(request: Request):
        if engine is None:
            raise CommerceError("database_unavailable", "Commerce storage is not configured", 503)

        def accept(body):
            try:
                assert_runtime_role(engine)
            except RuntimeError as exc:
                raise CommerceError("runtime_role_invalid", "Commerce storage is not ready", 503) from exc
            service = IngressService(engine, settings.ingress_secret.get_secret_value())
            return service.accept(
                body,
                request.headers.get("X-Commerce-Timestamp", ""),
                request.headers.get("X-Commerce-Signature", ""),
            )

        return await run_in_threadpool(accept, await request.body())

    for router in (catalog.router, conversations.router, orders.router, admin.router):
        app.include_router(router)
    return app
