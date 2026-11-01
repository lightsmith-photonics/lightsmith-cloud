"""FastAPI application and the ``lightsmith-server`` entry point.

    lightsmith-server                         # local mode: 127.0.0.1:8200, no auth, SQLite, in-process workers
    LIGHTSMITH_MODE=hosted lightsmith-server  # hosted: auth, quotas, Postgres, Redis queue (lightsmith-worker per GPU)
    lightsmith-server migrate                 # apply database migrations (hosted: run before the API and workers)
    lightsmith-server --openapi openapi.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .billing import PLANS
from .config import Settings
from .context import Context, build_context
from .db import Job, utcnow
from .errors import ApiError
from .executor import finish
from .routes import account, jobs
from .runners import LocalRunner, RqRunner

log = logging.getLogger("lightsmith_cloud")


class BodyLimit:
    """Reject request bodies above `max_bytes` (by Content-Length, and while streaming)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.max_bytes:
                return await _too_large(self.max_bytes, send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise ApiError(413, "PayloadTooLarge", f"request body above {self.max_bytes} bytes")
            return msg

        return await self.app(scope, limited, send)


async def _too_large(limit: int, send) -> None:
    body = json.dumps({"error": "PayloadTooLarge", "message": f"request body above {limit} bytes"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _reaper(ctx: Context) -> None:
    """Hosted: fail jobs whose worker vanished (running far beyond any runtime cap)."""
    grace = max(p.max_runtime_s for p in PLANS.values()) + 900
    while True:
        await asyncio.sleep(60)
        try:
            with ctx.db.session() as s:
                cutoff = utcnow() - timedelta(seconds=grace)
                for job in s.execute(select(Job).where(Job.status == "running", Job.started_at < cutoff)).scalars():
                    finish(s, job, "failed", {"error": "WorkerLost", "message": "the worker running this job stopped"})
        except Exception:
            log.exception("reaper failed")


def create_app(settings: Settings | None = None, *, ctx: Context | None = None) -> FastAPI:
    settings = settings or (ctx.settings if ctx else Settings())
    ctx = ctx or build_context(settings)
    if ctx.runner is None:
        ctx.runner = RqRunner(ctx, ctx.redis) if settings.hosted else LocalRunner(ctx, settings.local_workers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.runner.start()
        reaper = asyncio.create_task(_reaper(ctx)) if settings.hosted else None
        try:
            yield
        finally:
            if reaper:
                reaper.cancel()
            ctx.runner.stop()
            if ctx.estimator:
                ctx.estimator.close()

    app = FastAPI(
        title=f"{settings.service_name} API", version=__version__, lifespan=lifespan,
        description=f"{settings.service_name} job API. Local mode: single user, no auth. Hosted mode: "
                    "`Authorization: Bearer <api key | platform JWT>`. Errors are `{error, message}` JSON. "
                    "See API.md.")
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return JSONResponse(e.body(), status_code=e.status, headers=e.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError):
        errs = [{"loc": list(x.get("loc", ())), "msg": x.get("msg"), "type": x.get("type")} for x in e.errors()][:50]
        first = errs[0] if errs else {"loc": [], "msg": "invalid request"}
        msg = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
        return JSONResponse({"error": "ValidationError", "message": msg, "details": errs}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, e: StarletteHTTPException):
        name = {404: "NotFound", 405: "MethodNotAllowed"}.get(e.status_code, "HTTPError")
        return JSONResponse({"error": name, "message": str(e.detail)}, status_code=e.status_code,
                            headers=getattr(e, "headers", None))

    @app.exception_handler(Exception)
    async def _internal(request: Request, e: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "InternalError", "message": "internal server error"}, status_code=500)

    app.include_router(jobs.router)
    app.include_router(account.router)

    app.add_middleware(BodyLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=False, max_age=600)
    if not settings.hosted:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lightsmith-server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="bind address (default 127.0.0.1; local mode has no auth, keep it on loopback)")
    p.add_argument("--port", type=int, help="port (default 8200)")
    p.add_argument("--mode", choices=["local", "hosted"])
    p.add_argument("--workers", type=int, help="local mode: concurrent solver processes (default 1)")
    p.add_argument("--data-dir", help="local mode: SQLite database and results directory")
    p.add_argument("--openapi", metavar="PATH", help="write the OpenAPI document and exit")
    p.add_argument("--dry-run", action="store_true", help="stripe-setup: print what would be created, call nothing")
    p.add_argument("--no-portal", action="store_true", help="stripe-setup: skip the customer-portal configuration")
    p.add_argument("command", nargs="?", choices=["serve", "migrate", "stripe-setup", "billing-sync"], default="serve",
                   help="serve (default); migrate: upgrade the database schema and exit; stripe-setup: create the "
                        "Stripe test-mode meter, products, prices and portal configuration from STRIPE_SECRET_KEY "
                        "and print their ids; billing-sync: resend failed Stripe meter events (for cron)")
    a = p.parse_args(argv)

    over = {k: v for k, v in {"host": a.host, "port": a.port, "mode": a.mode, "local_workers": a.workers,
                              "data_dir": a.data_dir}.items() if v is not None}
    settings = Settings(**over)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if a.command == "migrate":
        from . import migrate
        from .db import Database

        db = Database(settings.db_url)
        migrate.upgrade(db.engine)
        print(json.dumps({"migrated": migrate.current_revision(db.engine)}))
        db.dispose()
        return 0
    if a.command == "stripe-setup":
        from . import stripe_setup

        return stripe_setup.main(settings, dry=a.dry_run, portal=not a.no_portal)
    if a.command == "billing-sync":
        from .billing import StripeBilling
        from .db import Database

        db = Database(settings.db_url)
        db.prepare(auto_migrate=False)
        with db.session() as s:
            print(json.dumps(StripeBilling(settings).sync_usage(s, limit=5000)))
        db.dispose()
        return 0
    if a.openapi:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            app = create_app(Settings(mode="local", data_dir=Path(d)))
            spec = app.openapi()
            app.state.ctx.db.dispose()
        Path(a.openapi).write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"written": a.openapi}))
        return 0
    if not settings.hosted and settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("local mode has no authentication; binding to %s exposes it to the network", settings.host)

    import uvicorn

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
