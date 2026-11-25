"""
卡密验证服务端
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

load_dotenv()

from api import router as api_router  # noqa: E402
from api import (  # noqa: E402
    DEVICE_BINDING_TTL,
    LicenseLocked,
    _signed_response,
    ONLINE_TIMEOUT,
    ensure_signing_key,
    validate_settings,
)
from db import DeviceBinding, HeartbeatLog, engine, init_db  # noqa: E402
from request_logging import RequestTimingMiddleware  # noqa: E402
from license_storage import ensure_display_key  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "1.4.5"
logger = logging.getLogger(__name__)
HEARTBEAT_LOG_RETENTION_DAYS = max(
    int(os.getenv("HEARTBEAT_LOG_RETENTION_DAYS", "1")), 1
)


async def _cleanup_once(sessions, *, now=None):
    now = now or datetime.now(timezone.utc)
    online_threshold = now - timedelta(seconds=ONLINE_TIMEOUT)
    binding_threshold = now - timedelta(seconds=DEVICE_BINDING_TTL)
    log_threshold = now - timedelta(days=HEARTBEAT_LOG_RETENTION_DAYS)
    async with sessions() as session:
        await session.execute(
            update(DeviceBinding)
            .where(
                DeviceBinding.last_heartbeat <= online_threshold,
                DeviceBinding.is_online.is_(True),
            )
            .values(is_online=False)
        )
        await session.execute(
            delete(DeviceBinding).where(
                DeviceBinding.last_heartbeat <= binding_threshold
            )
        )
        await session.execute(
            delete(HeartbeatLog).where(HeartbeatLog.created_at < log_threshold)
        )
        await session.commit()


async def _cleanup_loop():
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    while True:
        await asyncio.sleep(60)
        try:
            await _cleanup_once(sessions)
        except Exception:
            logger.exception("过期设备绑定清理失败，下轮重试")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()
    await init_db()
    ensure_signing_key()
    async with engine.connect() as connection:
        encrypted_count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM licenses WHERE encrypted_key IS NOT NULL")
            )
        ).scalar_one()
    ensure_display_key(existing_encrypted=bool(encrypted_count))
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await engine.dispose()


app = FastAPI(title="License Server", version=VERSION, lifespan=lifespan)
app.include_router(api_router)
app.add_middleware(RequestTimingMiddleware)


@app.exception_handler(LicenseLocked)
async def license_locked_handler(request, exc):
    return JSONResponse(
        status_code=403,
        content=_signed_response(
            {
                "ok": False,
                "blocked": True,
                "error": "该卡密连续验证失败10次，已锁定，请联系管理员解锁",
                "request_id": exc.request_id,
            }
        ),
    )


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"ok": True, "version": VERSION}


@app.get("/api/version")
async def get_version():
    return {"version": VERSION}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "9000"))
    # Our structured log deliberately omits raw URLs, queries and bodies.
    uvicorn.run(app, host=host, port=port, access_log=False)
