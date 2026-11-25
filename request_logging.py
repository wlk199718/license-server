"""Correlated, secret-free request timings without consuming request bodies."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request

from api import _client_ip


logger = logging.getLogger("uvicorn.error.license_requests")
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["http_request_id"] = request_id
        status_code = 500
        error_type = None

        async def timed_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Response-Time-Ms"] = (
                    f"{(time.perf_counter() - started) * 1000:.2f}"
                )
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except BaseException as exc:
            # No exception message/traceback: SQL parameters may contain cards.
            error_type = type(exc).__name__
            raise
        finally:
            route = getattr(scope.get("route"), "path", "[unmatched]")
            # route is the template (/admin/devices/{license_key}), not the
            # actual path. Never log the query, headers, body or card/device ID.
            event = {
                "event": "http_request",
                "request_id": request_id,
                "method": scope.get("method", "")[:16],
                "route": route,
                "status": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "client_ip": _client_ip(Request(scope)),
            }
            if error_type:
                event["error_type"] = error_type
            logger.info(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
