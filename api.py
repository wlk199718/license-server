"""
卡密验证服务端 API。

设计目标：
- 客户端响应使用 RSA 签名，客户端可以用内置公钥验证结果。
- 管理后台使用短期 HttpOnly 会话 Cookie，不再把管理员密钥放进 localStorage。
- 设备绑定在事务中完成，并由数据库唯一索引兜底。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db import (
    AuditLog,
    DeviceBinding,
    HeartbeatLog,
    License,
    LicenseCreateRequest,
    Project,
    get_db,
)
from license_storage import display_license, encrypt_license

router = APIRouter()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
SESSION_SECRET = (os.getenv("SESSION_SECRET") or ADMIN_KEY).encode("utf-8")
SESSION_TTL = _env_int("SESSION_TTL", 30 * 24 * 60 * 60)
HEARTBEAT_INTERVAL = _env_int("HEARTBEAT_INTERVAL", 60, minimum=15)
# The old variable remains a fallback for the online indicator only. It no
# longer changes heartbeat cadence or the lifetime of a device reservation.
ONLINE_TIMEOUT = _env_int("ONLINE_TIMEOUT", _env_int("HEARTBEAT_TIMEOUT", 180))
DEVICE_BINDING_TTL = _env_int("DEVICE_BINDING_TTL", 900)
VERIFY_IP_LIMIT = _env_int("VERIFY_IP_LIMIT", 1200)
HEARTBEAT_IP_LIMIT = _env_int("HEARTBEAT_IP_LIMIT", 6000)
VERIFY_DEVICE_LIMIT = _env_int("VERIFY_DEVICE_LIMIT", 12)
HEARTBEAT_DEVICE_LIMIT = _env_int("HEARTBEAT_DEVICE_LIMIT", 12)
INVALID_LICENSE_IP_LIMIT = _env_int("INVALID_LICENSE_IP_LIMIT", 120)
BLACKLIST_ERROR_LIMIT = 10
RESPONSE_TTL = _env_int("RESPONSE_TTL", 120)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
SIGNING_PRIVATE_KEY_PATH = Path(
    os.getenv(
        "SIGNING_PRIVATE_KEY_PATH",
        str(Path(__file__).resolve().parent / "data" / "license_private.pem"),
    )
)

_SIGNING_PRIVATE_KEY = None
_RATE_BUCKETS: dict[str, deque[float]] = {}
_RATE_WINDOWS: dict[str, int] = {}
_RATE_LOCK = threading.RLock()
_RATE_MAX_BUCKETS = 20_000


def validate_settings() -> None:
    if ONLINE_TIMEOUT < HEARTBEAT_INTERVAL * 2 or DEVICE_BINDING_TTL < ONLINE_TIMEOUT:
        raise RuntimeError(
            "ONLINE_TIMEOUT 必须至少为 HEARTBEAT_INTERVAL 的两倍，"
            "DEVICE_BINDING_TTL 不能小于 ONLINE_TIMEOUT"
        )
    if (
        len(ADMIN_KEY) < 32
        or ADMIN_KEY.startswith(("replace-", "your-"))
        or len(SESSION_SECRET) < 32
        or SESSION_SECRET == ADMIN_KEY.encode("utf-8")
    ):
        raise RuntimeError(
            "ADMIN_KEY 和 SESSION_SECRET 必须配置为两个不同的、至少 32 字符的随机值"
        )
    if ADMIN_PASSWORD_HASH:
        try:
            algorithm, rounds, salt, digest = ADMIN_PASSWORD_HASH.split("$")
            if (
                algorithm != "pbkdf2_sha256"
                or int(rounds) < 600_000
                or len(bytes.fromhex(salt)) < 16
                or len(bytes.fromhex(digest)) != 32
            ):
                raise ValueError("invalid hash")
        except ValueError as exc:
            raise RuntimeError("ADMIN_PASSWORD_HASH 格式无效") from exc


def _valid_admin_password(password: str) -> bool:
    if not ADMIN_PASSWORD_HASH:
        return _same_secret(password, ADMIN_KEY)
    try:
        _, rounds, salt, digest = ADMIN_PASSWORD_HASH.split("$")
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)
        )
        return hmac.compare_digest(computed, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    # The service is bound to localhost on the host, but the request appears
    # inside Docker as the bridge gateway (for example 172.18.0.1). Trust
    # forwarded client headers only from loopback/private proxy peers; the
    # published service is not exposed directly to the public network.
    trusted_proxy = peer in {"127.0.0.1", "::1"}
    if not trusted_proxy:
        try:
            trusted_proxy = ipaddress.ip_address(peer).is_private
        except ValueError:
            trusted_proxy = False
    if trusted_proxy:
        for header in ("X-Client-IP", "CF-Connecting-IP"):
            candidate = request.headers.get(header, "").strip()
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return candidate
    return peer


def _rate_limit(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    with _RATE_LOCK:
        if key not in _RATE_BUCKETS and len(_RATE_BUCKETS) >= 10_000:
            # Each bucket has its own window: a one-minute heartbeat must not
            # clear the five-minute administrator-login throttle.
            stale_keys = [
                bucket_key
                for bucket_key, values in _RATE_BUCKETS.items()
                if not values or now - values[-1] >= _RATE_WINDOWS[bucket_key]
            ]
            for stale_key in stale_keys:
                _RATE_BUCKETS.pop(stale_key, None)
                _RATE_WINDOWS.pop(stale_key, None)
        if key not in _RATE_BUCKETS and len(_RATE_BUCKETS) >= _RATE_MAX_BUCKETS:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后重试",
                headers={"Retry-After": str(window_seconds)},
            )
        bucket = _RATE_BUCKETS.setdefault(key, deque())
        _RATE_WINDOWS[key] = window_seconds
        while bucket and now - bucket[0] >= window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0]) + 0.999))
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def _device_rate_limit(event: str, license_id: str, device_id: str) -> None:
    # Called only after the full card secret was checked. Arbitrary device IDs
    # must not create authenticated buckets or reset another device's budget.
    identity = hashlib.sha256(
        json.dumps([license_id, device_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    limit = VERIFY_DEVICE_LIMIT if event == "verify" else HEARTBEAT_DEVICE_LIMIT
    _rate_limit(f"{event}-device:{identity}", limit, 60)


def _ensure_signing_private_key():
    global _SIGNING_PRIVATE_KEY
    if _SIGNING_PRIVATE_KEY is not None:
        return _SIGNING_PRIVATE_KEY

    SIGNING_PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SIGNING_PRIVATE_KEY_PATH.exists():
        _SIGNING_PRIVATE_KEY = serialization.load_pem_private_key(
            SIGNING_PRIVATE_KEY_PATH.read_bytes(), password=None
        )
        return _SIGNING_PRIVATE_KEY

    # Generate into the persistent data volume when the operator did not
    # inject a key. The private key is never baked into the Docker image.
    _SIGNING_PRIVATE_KEY = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    SIGNING_PRIVATE_KEY_PATH.write_bytes(
        _SIGNING_PRIVATE_KEY.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(SIGNING_PRIVATE_KEY_PATH, 0o600)
    except OSError:
        pass
    return _SIGNING_PRIVATE_KEY


def ensure_signing_key() -> None:
    _ensure_signing_private_key()


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sign_payload(payload: dict[str, Any]) -> str:
    signature = _ensure_signing_private_key().sign(
        _canonical_payload_bytes(payload),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _signed_response(payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    public_der = (
        _ensure_signing_private_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    data = dict(payload)
    data["signature_alg"] = "RS256"
    data["key_id"] = hashlib.sha256(public_der).hexdigest()[:16]
    data["signed_at"] = now.isoformat()
    data["response_expires_at"] = (now + timedelta(seconds=RESPONSE_TTL)).isoformat()
    data["signature"] = _sign_payload(data)
    return data


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _ensure_utc(value)
    return value.isoformat() if value else None


def _license_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _redact_license_key(raw_key: str) -> str:
    return raw_key.split(".", 1)[0][:16]


async def _find_client_license(
    db: AsyncSession, raw_key: str, *, for_update: bool = False
) -> License | None:
    if "." in raw_key:
        identifier, secret = raw_key.split(".", 1)
        if len(identifier) != 16 or len(secret) != 32:
            return None
        query = select(License).where(License.key == identifier)
        if for_update:
            query = query.with_for_update()
        lic = (await db.execute(query)).scalar_one_or_none()
        if (
            lic
            and lic.key_hash
            and hmac.compare_digest(lic.key_hash, _license_hash(raw_key))
        ):
            return lic
        return None

    # Old records retain their original key until the operator rotates them.
    query = select(License).where(License.key == raw_key, License.key_hash.is_(None))
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


class LicenseLocked(Exception):
    def __init__(self, request_id=None):
        self.request_id = request_id


def _license_identifier(raw_key: str) -> str:
    return raw_key.split(".", 1)[0][:16]


async def _find_license_identifier(db: AsyncSession, raw_key: str) -> License | None:
    identifier = _license_identifier(raw_key)
    if len(identifier) != 16:
        return None
    return (
        await db.execute(select(License).where(License.key == identifier))
    ).scalar_one_or_none()


async def _check_license_lock(db: AsyncSession, lic: License | None, request_id=None):
    if lic is not None and lic.locked_at is not None:
        raise LicenseLocked(request_id)


async def _register_license_error(db, request, raw_key, error):
    lic = await _find_license_identifier(db, raw_key)
    if lic is None:
        return False
    now = _utc_now()
    result = await db.execute(
        update(License)
        .where(License.id == lic.id, License.locked_at.is_(None))
        .values(
            failed_attempts=License.failed_attempts + 1,
            locked_at=case(
                (License.failed_attempts + 1 >= BLACKLIST_ERROR_LIMIT, now),
                else_=None,
            ),
        )
        .returning(License.failed_attempts, License.locked_at)
    )
    row = result.one_or_none()
    count, locked_at = row if row else (lic.failed_attempts, lic.locked_at)
    if count == BLACKLIST_ERROR_LIMIT:
        _audit(db, request, "license.lock", lic.key[:16])
    await db.commit()
    return locked_at is not None


async def _reset_license_errors(db, lic: License):
    lic.failed_attempts = 0


def _audit(db: AsyncSession, request: Request, action: str, target: str) -> None:
    db.add(AuditLog(action=action, target=target, actor_ip=_client_ip(request)))


def _add_heartbeat_log(
    db: AsyncSession,
    request: Request,
    *,
    event_type: str,
    license_key: str,
    device_id: str,
    project_code: str = "",
    ok: bool,
    device_info: str = "",
    error: str = "",
    request_id: str | None = None,
) -> None:
    db.add(
        HeartbeatLog(
            license_key=_redact_license_key(license_key),
            project_code=project_code[:64],
            device_id=device_id,
            event_type=event_type,
            ok=ok,
            client_ip=_client_ip(request),
            device_info=device_info[:512],
            error=error[:256],
            request_id=(request_id or "")[:128],
        )
    )


async def _record_client_failure(
    db: AsyncSession,
    request: Request,
    *,
    event_type: str,
    license_key: str,
    device_id: str,
    project_code: str = "",
    error: str,
    device_info: str = "",
    request_id: str | None = None,
    failed_secret: bool = False,
) -> None:
    """失败响应也落一条日志，但日志故障不能影响客户端收到原始错误。"""
    await db.rollback()
    blocked = False
    if failed_secret:
        blocked = await _register_license_error(db, request, license_key, error)
    try:
        if not project_code:
            known_license = await _find_client_license(db, license_key)
            project_code = known_license.project_code if known_license else ""
        _add_heartbeat_log(
            db,
            request,
            event_type=event_type,
            license_key=license_key,
            project_code=project_code,
            device_id=device_id,
            ok=False,
            device_info=device_info,
            error=error,
            request_id=request_id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
    if blocked:
        raise LicenseLocked(request_id)


def _same_secret(left: str, right: str) -> bool:
    return bool(left and right) and hmac.compare_digest(left.encode(), right.encode())


def _encode_session() -> str:
    payload = {
        "exp": int(time.time()) + SESSION_TTL,
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(SESSION_SECRET, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_session(token: str | None) -> bool:
    if not token or not SESSION_SECRET:
        return False
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(
            SESSION_SECRET, encoded.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return int(payload["exp"]) >= int(time.time())
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _set_admin_cookies(response: Response) -> None:
    response.set_cookie(
        "admin_session",
        _encode_session(),
        max_age=SESSION_TTL,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "csrf_token",
        secrets.token_urlsafe(24),
        max_age=SESSION_TTL,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


class VerifyRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    device_info: str = Field(default="", max_length=512)
    project: str = Field(default="", max_length=64)
    request_id: str | None = Field(default=None, max_length=128)

    @field_validator("device_id")
    @classmethod
    def device_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("设备标识不能为空")
        return value


class HeartbeatRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)

    @field_validator("device_id")
    @classmethod
    def device_must_not_be_blank(cls, value: str) -> str:
        return VerifyRequest.device_must_not_be_blank(value)


class AdminLoginRequest(BaseModel):
    admin_key: str = Field(min_length=1, max_length=256)


class CreateProjectRequest(BaseModel):
    code: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )
    name: str = Field(min_length=1, max_length=128)
    team: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    team: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str | None) -> str | None:
        return (
            CreateProjectRequest.name_must_not_be_blank(value)
            if value is not None
            else None
        )


class CreateLicenseRequest(BaseModel):
    project_code: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=256)
    max_devices: int = Field(default=1, ge=1, le=10_000)
    expires_days: int | None = Field(default=None, ge=1, le=36_500)
    starts_on_first_activation: bool = False
    count: int = Field(default=1, ge=1, le=100)
    request_id: str | None = Field(
        default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )


class RevokeLicenseRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)


class ActivateLicenseRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)


class DeleteLicenseRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)


class UpdateLicenseRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)
    note: str | None = Field(default=None, max_length=256)
    max_devices: int | None = Field(default=None, ge=1, le=10_000)


class UnbindDeviceRequest(BaseModel):
    license_key: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)


class UnblockClientRequest(BaseModel):
    license_identifier: str = Field(
        min_length=16, max_length=16, pattern=r"^[A-Za-z0-9_-]{16}$"
    )


async def verify_admin(
    request: Request,
    admin_session: str | None = Cookie(default=None),
    csrf_token: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
) -> None:
    session_valid = _valid_session(admin_session)
    legacy_valid = _same_secret(x_admin_key or "", ADMIN_KEY)
    if not session_valid and not legacy_valid:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    if session_valid and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if (
            not csrf_token
            or not x_csrf_token
            or not _same_secret(csrf_token, x_csrf_token)
        ):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")


@router.post("/admin/blacklist/unblock", dependencies=[Depends(verify_admin)])
async def unblock_client(
    req: UnblockClientRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        update(License)
        .where(
            License.key == req.license_identifier,
            License.locked_at.is_not(None),
        )
        .values(
            failed_attempts=0,
            locked_at=None,
        )
    )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="该卡密未锁定")
    _audit(db, request, "license.unlock", req.license_identifier)
    await db.commit()
    return {"ok": True}


@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request, response: Response):
    _rate_limit(f"admin-login:{_client_ip(request)}", 5, 300)
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="服务端未配置管理员密钥")
    if not _valid_admin_password(req.admin_key.strip()):
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    _set_admin_cookies(response)
    return {"ok": True, "session_ttl": SESSION_TTL}


@router.post("/admin/logout")
async def admin_logout(response: Response):
    response.delete_cookie("admin_session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"ok": True}


@router.get("/admin/me", dependencies=[Depends(verify_admin)])
async def admin_me():
    return {"ok": True}


@router.get("/api/public-key", response_class=PlainTextResponse)
async def public_key():
    public = _ensure_signing_private_key().public_key()
    return public.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


async def _begin_verify_transaction(db: AsyncSession) -> None:
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))
    else:
        await db.begin()


@router.post("/api/verify")
async def verify_license(
    req: VerifyRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """客户端启动时验证卡密并绑定设备。"""
    _rate_limit(f"verify:{_client_ip(request)}", VERIFY_IP_LIMIT, 60)
    license_key = req.license_key.strip()
    device_id = req.device_id.strip()
    await _begin_verify_transaction(db)
    try:
        lic = await _find_client_license(db, license_key, for_update=True)
        await _check_license_lock(
            db, lic or await _find_license_identifier(db, license_key), req.request_id
        )
        if not lic:
            _rate_limit(
                f"invalid-license:{_client_ip(request)}", INVALID_LICENSE_IP_LIMIT, 60
            )
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="卡密不存在",
                request_id=req.request_id,
                failed_secret=True,
            )
            return _signed_response(
                {"ok": False, "error": "卡密不存在", "request_id": req.request_id}
            )
        _device_rate_limit("verify", lic.key, device_id)
        if not lic.is_active:
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="卡密已被吊销",
                request_id=req.request_id,
            )
            return _signed_response(
                {"ok": False, "error": "卡密已被吊销", "request_id": req.request_id}
            )

        if lic.duration_days is not None and lic.activated_at is None:
            lic.activated_at = _utc_now()
            lic.expires_at = lic.activated_at + timedelta(days=lic.duration_days)
            await db.flush()

        expires_at = _ensure_utc(lic.expires_at)
        if expires_at and _utc_now() > expires_at:
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="卡密已过期",
                request_id=req.request_id,
            )
            return _signed_response(
                {"ok": False, "error": "卡密已过期", "request_id": req.request_id}
            )

        project_code = req.project.strip()
        if project_code and lic.project_code != project_code:
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="卡密与项目不匹配",
                request_id=req.request_id,
            )
            return _signed_response(
                {"ok": False, "error": "卡密与项目不匹配", "request_id": req.request_id}
            )

        result = await db.execute(
            select(Project).where(Project.code == lic.project_code)
        )
        proj = result.scalar_one_or_none()
        if not proj:
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="卡密所属项目不存在",
                request_id=req.request_id,
            )
            return _signed_response(
                {
                    "ok": False,
                    "error": "卡密所属项目不存在",
                    "request_id": req.request_id,
                }
            )
        if not proj.is_active:
            await _record_client_failure(
                db,
                request,
                event_type="verify",
                license_key=license_key,
                device_id=device_id,
                device_info=req.device_info,
                error="该项目已停用",
                request_id=req.request_id,
            )
            return _signed_response(
                {"ok": False, "error": "该项目已停用", "request_id": req.request_id}
            )

        binding_key = lic.key
        timeout_threshold = _utc_now() - timedelta(seconds=DEVICE_BINDING_TTL)
        await db.execute(
            delete(DeviceBinding).where(
                DeviceBinding.license_key == binding_key,
                DeviceBinding.last_heartbeat <= timeout_threshold,
            )
        )
        result = await db.execute(
            select(DeviceBinding).where(
                DeviceBinding.license_key == binding_key,
                DeviceBinding.device_id == device_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.last_heartbeat = _utc_now()
            existing.is_online = True
            existing.last_ip = _client_ip(request)
            if req.device_info.strip():
                existing.device_info = req.device_info.strip()
        else:
            current_count = (
                await db.execute(
                    select(func.count(DeviceBinding.id)).where(
                        DeviceBinding.license_key == binding_key
                    )
                )
            ).scalar_one()
            if current_count >= (lic.max_devices or 1):
                max_devices = lic.max_devices
                error = f"设备数已达上限 ({current_count}/{max_devices})"
                await _record_client_failure(
                    db,
                    request,
                    event_type="verify",
                    license_key=license_key,
                    device_id=device_id,
                    device_info=req.device_info,
                    error=error,
                    request_id=req.request_id,
                )
                return _signed_response(
                    {
                        "ok": False,
                        "error": error,
                        "request_id": req.request_id,
                    }
                )
            db.add(
                DeviceBinding(
                    license_key=binding_key,
                    device_id=device_id,
                    device_info=req.device_info.strip(),
                    last_ip=_client_ip(request),
                )
            )

        _add_heartbeat_log(
            db,
            request,
            event_type="verify",
            license_key=binding_key,
            project_code=lic.project_code,
            device_id=device_id,
            ok=True,
            device_info=req.device_info.strip(),
            request_id=req.request_id,
        )
        await _reset_license_errors(db, lic)
        await db.commit()
        return _signed_response(
            {
                "ok": True,
                "request_id": req.request_id,
                "heartbeat_interval": HEARTBEAT_INTERVAL,
                "expires_at": _iso(expires_at),
                "max_devices": lic.max_devices,
                "project": lic.project_code,
                "device_id": device_id,
            }
        )
    except IntegrityError:
        await _record_client_failure(
            db,
            request,
            event_type="verify",
            license_key=license_key,
            device_id=device_id,
            device_info=req.device_info,
            error="设备绑定冲突，请重试",
            request_id=req.request_id,
        )
        return _signed_response(
            {
                "ok": False,
                "error": "设备绑定冲突，请重试",
                "request_id": req.request_id,
            }
        )


@router.post("/api/heartbeat")
async def heartbeat(
    req: HeartbeatRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """客户端定时心跳。"""
    _rate_limit(f"heartbeat:{_client_ip(request)}", HEARTBEAT_IP_LIMIT, 60)
    license_key = req.license_key.strip()
    device_id = req.device_id.strip()
    lic = await _find_client_license(db, license_key)
    await _check_license_lock(
        db, lic or await _find_license_identifier(db, license_key), req.request_id
    )
    if lic:
        _device_rate_limit("heartbeat", lic.key, device_id)
    else:
        _rate_limit(
            f"invalid-license:{_client_ip(request)}", INVALID_LICENSE_IP_LIMIT, 60
        )
    if not lic or not lic.is_active:
        await _record_client_failure(
            db,
            request,
            event_type="heartbeat",
            license_key=license_key,
            device_id=device_id,
            error="卡密无效或已吊销",
            request_id=req.request_id,
            failed_secret=not lic,
        )
        return _signed_response(
            {"ok": False, "error": "卡密无效或已吊销", "request_id": req.request_id}
        )
    expires_at = _ensure_utc(lic.expires_at)
    if expires_at and _utc_now() > expires_at:
        await _record_client_failure(
            db,
            request,
            event_type="heartbeat",
            license_key=license_key,
            device_id=device_id,
            error="卡密已过期",
            request_id=req.request_id,
        )
        return _signed_response(
            {"ok": False, "error": "卡密已过期", "request_id": req.request_id}
        )

    project = (
        await db.execute(select(Project).where(Project.code == lic.project_code))
    ).scalar_one_or_none()
    if not project or not project.is_active:
        await _record_client_failure(
            db,
            request,
            event_type="heartbeat",
            license_key=license_key,
            device_id=device_id,
            error="该项目已停用",
            request_id=req.request_id,
        )
        return _signed_response(
            {"ok": False, "error": "该项目已停用", "request_id": req.request_id}
        )

    result = await db.execute(
        select(DeviceBinding).where(
            DeviceBinding.license_key == lic.key,
            DeviceBinding.device_id == device_id,
        )
    )
    binding = result.scalar_one_or_none()
    last_heartbeat = _ensure_utc(binding.last_heartbeat) if binding else None
    if not last_heartbeat or last_heartbeat <= _utc_now() - timedelta(
        seconds=DEVICE_BINDING_TTL
    ):
        await _record_client_failure(
            db,
            request,
            event_type="heartbeat",
            license_key=license_key,
            device_id=device_id,
            error="设备未注册，请重新启动程序",
            request_id=req.request_id,
        )
        return _signed_response(
            {
                "ok": False,
                "error": "设备未注册，请重新启动程序",
                "request_id": req.request_id,
            }
        )

    binding.last_heartbeat = _utc_now()
    binding.is_online = True
    binding.last_ip = _client_ip(request)
    _add_heartbeat_log(
        db,
        request,
        event_type="heartbeat",
        license_key=lic.key,
        project_code=lic.project_code,
        device_id=device_id,
        ok=True,
        request_id=req.request_id,
    )
    await _reset_license_errors(db, lic)
    await db.commit()
    return _signed_response(
        {
            "ok": True,
            "license_key": lic.key,
            "device_id": device_id,
            "project": lic.project_code,
            "request_id": req.request_id,
        }
    )


@router.post("/admin/projects", dependencies=[Depends(verify_admin)])
async def create_project(
    req: CreateProjectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    project_code = req.code.strip()
    result = await db.execute(select(Project).where(Project.code == project_code))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="项目标识已存在")
    proj = Project(
        code=project_code,
        name=req.name.strip(),
        team=req.team.strip(),
        description=req.description.strip(),
    )
    db.add(proj)
    _audit(db, request, "project.create", project_code)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="项目标识已存在") from None
    return {
        "ok": True,
        "project": {"code": proj.code, "name": proj.name, "team": proj.team or ""},
    }


@router.get("/admin/projects", dependencies=[Depends(verify_admin)])
async def list_projects(db: AsyncSession = Depends(get_db)):
    now = _utc_now()
    counts = (
        select(
            License.project_code.label("project_code"),
            func.count(License.id).label("license_count"),
            func.sum(
                case(
                    (
                        License.is_active.is_(True)
                        & (License.expires_at.is_(None) | (License.expires_at > now)),
                        1,
                    ),
                    else_=0,
                )
            ).label("active_license_count"),
        )
        .group_by(License.project_code)
        .subquery()
    )
    result = await db.execute(
        select(
            Project,
            func.coalesce(counts.c.license_count, 0),
            func.coalesce(counts.c.active_license_count, 0),
        )
        .outerjoin(counts, counts.c.project_code == Project.code)
        .order_by(Project.created_at.desc(), Project.id.desc())
    )
    items = []
    for project, license_count, active_count in result.all():
        items.append(
            {
                "code": project.code,
                "name": project.name,
                "team": project.team or "",
                "description": project.description or "",
                "is_active": bool(project.is_active),
                "license_count": int(license_count or 0),
                "active_license_count": int(active_count or 0),
                "created_at": _iso(project.created_at),
            }
        )
    return {"ok": True, "projects": items}


@router.get("/admin/stats", dependencies=[Depends(verify_admin)])
async def get_stats(db: AsyncSession = Depends(get_db)):
    now = _utc_now()
    online_threshold = now - timedelta(seconds=ONLINE_TIMEOUT)
    total = (await db.execute(select(func.count(License.id)))).scalar_one()
    active = (
        await db.execute(
            select(func.count(License.id)).where(
                License.is_active.is_(True),
                (License.expires_at.is_(None) | (License.expires_at > now)),
            )
        )
    ).scalar_one()
    expired = (
        await db.execute(
            select(func.count(License.id)).where(
                License.is_active.is_(True),
                License.expires_at.is_not(None),
                License.expires_at <= now,
            )
        )
    ).scalar_one()
    revoked = (
        await db.execute(
            select(func.count(License.id)).where(License.is_active.is_(False))
        )
    ).scalar_one()
    device_count = (
        await db.execute(
            select(func.count(DeviceBinding.id)).where(
                DeviceBinding.last_heartbeat > online_threshold
            )
        )
    ).scalar_one()
    return {
        "ok": True,
        "total_licenses": int(total or 0),
        "active_licenses": int(active or 0),
        "expired_licenses": int(expired or 0),
        "revoked_licenses": int(revoked or 0),
        "bound_devices": int(device_count or 0),
    }


@router.get("/admin/audit", dependencies=[Depends(verify_admin)])
async def list_audit(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(AuditLog.id)))).scalar_one()
    result = await db.execute(
        select(AuditLog)
        .order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "ok": True,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
        "items": [
            {
                "action": entry.action,
                "target": entry.target,
                "actor_ip": entry.actor_ip,
                "created_at": _iso(entry.created_at),
            }
            for entry in result.scalars()
        ],
    }


@router.get("/admin/heartbeat-logs", dependencies=[Depends(verify_admin)])
async def list_heartbeat_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    license_key: str | None = Query(default=None, max_length=128),
    device_id: str | None = Query(default=None, max_length=128),
    client_ip: str | None = Query(default=None, max_length=64),
    event_type: str | None = Query(default=None, max_length=16),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if license_key and license_key.strip():
        filters.append(
            HeartbeatLog.license_key == _redact_license_key(license_key.strip())
        )
    if device_id and device_id.strip():
        filters.append(HeartbeatLog.device_id.contains(device_id.strip()))
    if client_ip and client_ip.strip():
        filters.append(HeartbeatLog.client_ip.contains(client_ip.strip()))
    if event_type and event_type.strip():
        if event_type.strip() not in {"verify", "heartbeat"}:
            raise HTTPException(status_code=400, detail="日志类型无效")
        filters.append(HeartbeatLog.event_type == event_type.strip())

    total = (
        await db.execute(select(func.count(HeartbeatLog.id)).where(*filters))
    ).scalar_one()
    online_threshold = _utc_now() - timedelta(seconds=ONLINE_TIMEOUT)
    summary_filters = [DeviceBinding.last_heartbeat > online_threshold]
    if device_id and device_id.strip():
        summary_filters.append(DeviceBinding.device_id.contains(device_id.strip()))
    if client_ip and client_ip.strip():
        summary_filters.append(DeviceBinding.last_ip.contains(client_ip.strip()))
    summary_result = await db.execute(
        select(
            License.project_code,
            License.note,
            func.count(func.distinct(DeviceBinding.device_id)).label("count"),
        )
        .join(DeviceBinding, DeviceBinding.license_key == License.key)
        .where(
            *summary_filters,
            License.is_active.is_(True),
            License.expires_at.is_(None) | (License.expires_at > _utc_now()),
            func.trim(License.project_code) != "",
        )
        .group_by(License.project_code, License.note)
    )
    summary = [
        {
            "project_code": project_code,
            "license_note": note.strip() if note and note.strip() else "无备注",
            "count": int(count or 0),
        }
        for project_code, note, count in summary_result.all()
    ]
    summary.sort(
        key=lambda item: (-item["count"], item["project_code"], item["license_note"])
    )
    online_device_count = sum(item["count"] for item in summary)
    blocked_rows = (
        (
            await db.execute(
                select(License)
                .where(License.locked_at.is_not(None))
                .order_by(License.locked_at.desc(), License.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    result = await db.execute(
        select(HeartbeatLog)
        .where(*filters)
        .order_by(HeartbeatLog.created_at.desc(), HeartbeatLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    entries = list(result.scalars())
    # Logs intentionally keep only the first 16 characters of a card key.
    # This also covers older 32-character plaintext keys without exposing
    # their full value. Do not assign a note if prefixes are ambiguous.
    notes_by_key: dict[str, str | None] = {}
    projects_by_key: dict[str, str | None] = {}
    if entries:
        display_key = func.substr(License.key, 1, 16)
        note_rows = await db.execute(
            select(display_key, License.note, License.project_code).where(
                display_key.in_({entry.license_key for entry in entries})
            )
        )
        for key, note, project_code in note_rows:
            if key in notes_by_key:
                notes_by_key[key] = None
                projects_by_key[key] = None
            else:
                notes_by_key[key] = note or ""
                projects_by_key[key] = project_code
    return {
        "ok": True,
        "total": int(total or 0),
        "total_pages": (total + page_size - 1) // page_size,
        "summary": summary,
        "online_device_count": online_device_count,
        "online_timeout_seconds": ONLINE_TIMEOUT,
        "blacklist": [
            {
                "license_identifier": item.key[:16],
                "project_code": item.project_code,
                "license_note": item.note or "",
                "failed_attempts": item.failed_attempts,
                "blocked_at": _iso(item.locked_at),
            }
            for item in blocked_rows
        ],
        "items": [
            {
                "id": entry.id,
                "license_key": entry.license_key,
                "project_code": entry.project_code
                or (
                    projects_by_key.get(entry.license_key) or ""
                    if entry.ok or entry.error not in {"卡密不存在", "卡密无效或已吊销"}
                    else ""
                ),
                "device_id": entry.device_id,
                "event_type": entry.event_type,
                "ok": bool(entry.ok),
                "client_ip": entry.client_ip or "",
                "device_info": entry.device_info or "",
                "license_note": (
                    notes_by_key.get(entry.license_key) or ""
                    if entry.ok or entry.error not in {"卡密不存在", "卡密无效或已吊销"}
                    else ""
                ),
                "error": entry.error or "",
                "request_id": entry.request_id or "",
                "created_at": _iso(entry.created_at),
            }
            for entry in entries
        ],
    }


@router.put("/admin/projects/{code}", dependencies=[Depends(verify_admin)])
async def update_project(
    code: str,
    req: UpdateProjectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Project).where(Project.code == code))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    if req.name is not None:
        proj.name = req.name.strip()
    if req.team is not None:
        proj.team = req.team.strip()
    if req.description is not None:
        proj.description = req.description.strip()
    if req.is_active is not None:
        proj.is_active = req.is_active
    _audit(db, request, "project.update", code)
    await db.commit()
    return {"ok": True, "message": "项目已更新"}


@router.delete("/admin/projects/{code}", dependencies=[Depends(verify_admin)])
async def delete_project(
    code: str,
    request: Request,
    expected_count: int = Query(ge=0),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.code == code).with_for_update()
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="项目不存在")
    count = (
        await db.execute(
            select(func.count(License.id)).where(License.project_code == code)
        )
    ).scalar_one()
    if count != expected_count:
        raise HTTPException(
            status_code=409,
            detail=f"卡密数已变化（当前 {count} 张），请刷新后重新确认删除",
        )
    license_keys = select(License.key).where(License.project_code == code)
    await db.execute(
        delete(DeviceBinding).where(DeviceBinding.license_key.in_(license_keys))
    )
    await db.execute(delete(License).where(License.project_code == code))
    await db.execute(delete(Project).where(Project.code == code))
    _audit(db, request, "project.delete", code)
    await db.commit()
    return {"ok": True, "message": "项目已删除"}


@router.post("/admin/licenses", dependencies=[Depends(verify_admin)])
async def create_licenses(
    req: CreateLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    project_code = req.project_code.strip()
    result = await db.execute(
        select(Project).where(Project.code == project_code).with_for_update()
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="项目不存在，请先创建项目")
    expires_at = (
        _utc_now() + timedelta(days=req.expires_days)
        if req.expires_days is not None and not req.starts_on_first_activation
        else None
    )
    if req.starts_on_first_activation and req.expires_days is None:
        raise HTTPException(status_code=422, detail="首次激活计时需要设置有效期")
    if req.request_id:
        # Keep the existing idempotency contract: duplicates do not create cards.
        # Administrators can now recover their full cards from the listing.
        db.add(LicenseCreateRequest(request_id=req.request_id))
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="这次生成请求已处理，请到卡密列表查看和复制，勿重复生成",
            ) from exc
    keys = []
    for _ in range(req.count):
        identifier = secrets.token_hex(8)
        key = f"{identifier}.{secrets.token_hex(16)}"
        while key in keys:
            identifier = secrets.token_hex(8)
            key = f"{identifier}.{secrets.token_hex(16)}"
        db.add(
            License(
                key=identifier,
                key_hash=_license_hash(key),
                encrypted_key=encrypt_license(key),
                project_code=project_code,
                note=req.note.strip(),
                max_devices=req.max_devices,
                expires_at=expires_at,
                duration_days=req.expires_days
                if req.starts_on_first_activation
                else None,
            )
        )
        keys.append(key)
    _audit(db, request, "license.create", f"{project_code}:{req.count}")
    await db.commit()
    return {
        "ok": True,
        "licenses": keys,
        "expires_at": _iso(expires_at),
        "max_devices": req.max_devices,
    }


@router.get("/admin/licenses", dependencies=[Depends(verify_admin)])
async def list_licenses(
    project: str | None = Query(default=None, max_length=64),
    search: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, pattern="^(active|expired|revoked)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    count_query = select(func.count(License.id)).select_from(License)
    data_query = select(
        License,
        func.coalesce(
            select(func.count(DeviceBinding.id))
            .where(
                DeviceBinding.license_key == License.key,
                DeviceBinding.last_heartbeat
                > _utc_now() - timedelta(seconds=ONLINE_TIMEOUT),
            )
            .scalar_subquery(),
            0,
        ).label("device_count"),
    ).order_by(License.created_at.desc(), License.id.desc())
    if project:
        project = project.strip()
        count_query = count_query.where(License.project_code == project)
        data_query = data_query.where(License.project_code == project)
    if search and (term := search.strip()):
        # Full-card search uses its existing hash, never a plaintext DB column.
        match = License.key.contains(term, autoescape=True) | License.note.contains(
            term, autoescape=True
        )
        if "." in term:
            match = match | (License.key_hash == _license_hash(term))
        count_query = count_query.where(match)
        data_query = data_query.where(match)
    if status:
        now = _utc_now()
        if status == "revoked":
            condition = License.is_active.is_(False)
        elif status == "expired":
            condition = License.is_active.is_(True) & (License.expires_at <= now)
        else:
            condition = License.is_active.is_(True) & (
                License.expires_at.is_(None) | (License.expires_at > now)
            )
        count_query = count_query.where(condition)
        data_query = data_query.where(condition)
    total = (await db.execute(count_query)).scalar_one()
    result = await db.execute(
        data_query.offset((page - 1) * page_size).limit(page_size)
    )
    items = []
    for lic, device_count in result.all():
        try:
            full_key = display_license(lic)
            display_error = "" if full_key else "旧卡密仅保存摘要，无法还原"
        except RuntimeError:
            full_key = None
            display_error = "展示密钥或加密数据异常，请联系管理员恢复备份"
        items.append(
            {
                "key": lic.key,
                "full_key": full_key,
                "display_error": display_error,
                "is_legacy": lic.key_hash is None,
                "project_code": lic.project_code,
                "note": lic.note or "",
                "max_devices": lic.max_devices,
                "current_devices": int(device_count or 0),
                "expires_at": _iso(lic.expires_at),
                "duration_days": lic.duration_days,
                "activated_at": _iso(lic.activated_at),
                "is_active": bool(lic.is_active),
                "created_at": _iso(lic.created_at),
            }
        )
    return {
        "ok": True,
        "licenses": items,
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.post("/admin/revoke", dependencies=[Depends(verify_admin)])
async def revoke_license(
    req: RevokeLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")
    lic.is_active = False
    await db.execute(
        delete(DeviceBinding).where(DeviceBinding.license_key == req.license_key)
    )
    _audit(db, request, "license.revoke", _redact_license_key(req.license_key))
    await db.commit()
    return {"ok": True, "message": "卡密已吊销"}


@router.post("/admin/activate", dependencies=[Depends(verify_admin)])
async def activate_license(
    req: ActivateLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")
    lic.is_active = True
    _audit(db, request, "license.activate", _redact_license_key(req.license_key))
    await db.commit()
    return {"ok": True, "message": "卡密已启用"}


@router.post("/admin/delete", dependencies=[Depends(verify_admin)])
async def delete_license(
    req: DeleteLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(License).where(License.key == req.license_key))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="卡密不存在")
    await db.execute(
        delete(DeviceBinding).where(DeviceBinding.license_key == req.license_key)
    )
    await db.execute(delete(License).where(License.key == req.license_key))
    _audit(db, request, "license.delete", _redact_license_key(req.license_key))
    await db.commit()
    return {"ok": True, "message": "卡密已删除"}


@router.post("/admin/update", dependencies=[Depends(verify_admin)])
async def update_license(
    req: UpdateLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")
    if req.max_devices is not None:
        current_devices = (
            await db.execute(
                select(func.count(DeviceBinding.id)).where(
                    DeviceBinding.license_key == req.license_key
                )
            )
        ).scalar_one()
        if req.max_devices < current_devices:
            raise HTTPException(
                status_code=400,
                detail=f"最大设备数不能低于当前绑定数 ({current_devices})",
            )
        lic.max_devices = req.max_devices
    if req.note is not None:
        lic.note = req.note.strip()
    _audit(db, request, "license.update", _redact_license_key(req.license_key))
    await db.commit()
    return {"ok": True, "message": "卡密已更新"}


@router.post("/admin/unbind", dependencies=[Depends(verify_admin)])
async def unbind_device(
    req: UnbindDeviceRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        delete(DeviceBinding).where(
            DeviceBinding.license_key == req.license_key,
            DeviceBinding.device_id == req.device_id,
        )
    )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="未找到该设备绑定")
    _audit(
        db,
        request,
        "device.unbind",
        f"{_redact_license_key(req.license_key)}:{req.device_id}",
    )
    await db.commit()
    return {"ok": True, "message": "设备已解绑"}


@router.get("/admin/devices/{license_key}", dependencies=[Depends(verify_admin)])
async def list_devices(license_key: str, db: AsyncSession = Depends(get_db)):
    license_key = _redact_license_key(license_key)
    result = await db.execute(select(License).where(License.key == license_key))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="卡密不存在")
    result = await db.execute(
        select(DeviceBinding)
        .where(DeviceBinding.license_key == license_key)
        .order_by(DeviceBinding.last_heartbeat.desc())
    )
    timeout_threshold = _utc_now() - timedelta(seconds=ONLINE_TIMEOUT)
    items = []
    for device in result.scalars().all():
        last_heartbeat = _ensure_utc(device.last_heartbeat)
        items.append(
            {
                "device_id": device.device_id,
                "device_info": device.device_info or "",
                "last_ip": device.last_ip or "",
                "first_seen": _iso(device.first_seen),
                "last_heartbeat": _iso(last_heartbeat),
                "is_online": bool(
                    last_heartbeat and last_heartbeat > timeout_threshold
                ),
            }
        )
    return {"ok": True, "devices": items}
