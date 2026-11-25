"""Client-side verification of signed license responses.

Bundle a trusted public PEM with the client. Never download a fresh public key
from the same untrusted response channel and treat it as an authority.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class InvalidLicenseResponse(ValueError):
    """The response did not pass signature, freshness or request matching."""


def verify_response(
    response: dict[str, Any],
    public_key_pem: bytes,
    *,
    request_id: str,
    device_id: str,
    expected_project: str | None = None,
    max_clock_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Validate a server response against a pinned public key.

    Returns the authenticated payload (without the signature) even if ``ok``
    is false. Callers must explicitly reject an authenticated failure result.
    """
    try:
        payload = dict(response)
        signature = base64.b64decode(payload.pop("signature"), validate=True)
        public_key = serialization.load_pem_public_key(public_key_pem)
        public_der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        expected_key_id = hashlib.sha256(public_der).hexdigest()[:16]
        if payload["signature_alg"] != "RS256":
            raise InvalidLicenseResponse("不支持的签名算法")
        if not hmac.compare_digest(payload["key_id"], expected_key_id):
            raise InvalidLicenseResponse("签名公钥不匹配")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        public_key.verify(signature, canonical, padding.PKCS1v15(), hashes.SHA256())
        if not hmac.compare_digest(payload["request_id"], request_id):
            raise InvalidLicenseResponse("请求标识不匹配")
        now = datetime.now(timezone.utc)
        issued = datetime.fromisoformat(payload["signed_at"])
        expires = datetime.fromisoformat(payload["response_expires_at"])
        skew = timedelta(seconds=max_clock_skew_seconds)
        if issued.tzinfo is None or expires.tzinfo is None:
            raise InvalidLicenseResponse("响应时间缺少时区")
        if issued > now + skew or expires < now - skew or expires <= issued:
            raise InvalidLicenseResponse("响应已过期或服务端时间异常")
        if payload.get("ok") is True:
            if not hmac.compare_digest(payload["device_id"], device_id):
                raise InvalidLicenseResponse("设备标识不匹配")
            if (
                expected_project is not None
                and payload.get("project") != expected_project
            ):
                raise InvalidLicenseResponse("项目标识不匹配")
        return payload
    except InvalidLicenseResponse:
        raise
    except Exception as exc:
        raise InvalidLicenseResponse("响应签名或结构校验失败") from exc
