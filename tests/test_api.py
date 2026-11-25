import asyncio
import base64
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api
from client_verifier import InvalidLicenseResponse, verify_response
import db
import main
import license_storage
from main import app


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        test_engine = create_async_engine(
            f"sqlite+aiosqlite:///{(path / 'test.db').as_posix()}",
            connect_args={"timeout": 30},
        )
        maker = async_sessionmaker(test_engine, expire_on_commit=False)

        async def override_db():
            async with maker() as session:
                yield session

        async def initialize():
            async with test_engine.begin() as conn:
                await conn.run_sync(db.Base.metadata.create_all)

        asyncio.run(initialize())
        app.dependency_overrides[db.get_db] = override_db

        async def no_op_init():
            pass

        monkeypatch.setattr(main, "init_db", no_op_init)
        monkeypatch.setattr(main, "engine", test_engine)
        monkeypatch.setattr(api, "ADMIN_KEY", "test-admin-key-with-at-least-32-chars")
        monkeypatch.setattr(
            api, "SESSION_SECRET", b"separate-test-session-secret-with-32-chars"
        )
        monkeypatch.setattr(api, "SIGNING_PRIVATE_KEY_PATH", path / "test-key.pem")
        monkeypatch.setattr(api, "_SIGNING_PRIVATE_KEY", None)
        monkeypatch.setattr(license_storage, "DISPLAY_KEY_PATH", path / "display.key")
        monkeypatch.setattr(license_storage, "_cipher", None)
        api._RATE_BUCKETS.clear()
        api._RATE_WINDOWS.clear()
        with TestClient(app) as test_client:
            test_client.test_db_path = path / "test.db"
            yield test_client
        app.dependency_overrides.clear()
        asyncio.run(test_engine.dispose())


def login(client):
    response = client.post(
        "/admin/login",
        json={"admin_key": "test-admin-key-with-at-least-32-chars"},
    )
    assert response.status_code == 200
    token = client.cookies.get("csrf_token")
    assert token
    return {"X-CSRF-Token": token}


def create_license(client, headers, *, count=1, max_devices=1, note=""):
    response = client.post(
        "/admin/projects",
        json={"code": "test_project", "name": "Test Project"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/admin/licenses",
        json={
            "project_code": "test_project",
            "count": count,
            "max_devices": max_devices,
            "note": note,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["licenses"]


def test_heartbeat_log_uses_current_license_note_for_legacy_keys(client):
    import sqlite3

    headers = login(client)
    assert (
        client.post(
            "/admin/projects",
            json={"code": "test_project", "name": "Test Project"},
            headers=headers,
        ).status_code
        == 200
    )
    legacy_key = "1234567890abcdef1234567890abcdef"
    connection = sqlite3.connect(client.test_db_path)
    try:
        connection.execute(
            "INSERT INTO licenses (key, project_code, note, max_devices, is_active) "
            "VALUES (?, 'test_project', ?, 1, 1)",
            (legacy_key, "第一组"),
        )
        connection.commit()
    finally:
        connection.close()

    payload = {
        "license_key": legacy_key,
        "device_id": "legacy-device",
        "project": "test_project",
    }
    assert client.post("/api/verify", json=payload).json()["ok"] is True
    logs = client.get("/admin/heartbeat-logs").json()["items"]
    assert logs[0]["license_key"] == legacy_key[:16]
    assert logs[0]["project_code"] == "test_project"
    assert logs[0]["license_note"] == "第一组"
    assert legacy_key not in str(logs)
    assert client.get("/admin/heartbeat-logs").json()["summary"] == [
        {
            "project_code": "test_project",
            "license_note": "第一组",
            "count": 1,
        }
    ]

    connection = sqlite3.connect(client.test_db_path)
    try:
        connection.execute(
            "UPDATE licenses SET note = ? WHERE key = ?", ("第二组", legacy_key)
        )
        connection.commit()
    finally:
        connection.close()
    assert (
        client.get("/admin/heartbeat-logs").json()["items"][0]["license_note"]
        == "第二组"
    )

    wrong_key = legacy_key[:16] + "0" * 16
    assert (
        client.post("/api/verify", json={**payload, "license_key": wrong_key}).json()[
            "ok"
        ]
        is False
    )
    assert client.get("/admin/heartbeat-logs").json()["items"][0]["license_note"] == ""


def test_heartbeat_log_note_for_new_key_and_ambiguous_prefix(client):
    import sqlite3

    headers = login(client)
    key = create_license(client, headers, note="销售一组")[0]
    identifier = key.split(".")[0]
    payload = {"license_key": key, "device_id": "device-new", "project": "test_project"}
    assert client.post("/api/verify", json=payload).json()["ok"] is True
    logs = client.get("/admin/heartbeat-logs").json()["items"]
    assert logs[0]["license_key"] == identifier
    assert logs[0]["project_code"] == "test_project"
    assert logs[0]["license_note"] == "销售一组"

    connection = sqlite3.connect(client.test_db_path)
    try:
        connection.execute(
            "INSERT INTO licenses (key, project_code, note, max_devices, is_active) "
            "VALUES (?, 'test_project', '碰撞分组', 1, 1)",
            (identifier + "different-legacy-key",),
        )
        connection.commit()
    finally:
        connection.close()
    assert client.get("/admin/heartbeat-logs").json()["items"][0]["license_note"] == ""


def test_unknown_card_requests_stay_in_logs_but_not_project_summary(client):
    headers = login(client)
    key = create_license(client, headers, note="销售一组")[0]
    payload = {
        "license_key": key,
        "device_id": "known-device",
        "project": "test_project",
    }
    assert client.post("/api/verify", json=payload).json()["ok"]
    # Forging the secret for a real identifier must not add a client to its group.
    wrong_key = key.split(".")[0] + "." + ("0" if key[-1] != "0" else "1") * 32
    for wrong in (wrong_key, "unknown-card"):
        assert not client.post(
            "/api/verify",
            json={**payload, "license_key": wrong, "device_id": "unknown-device"},
        ).json()["ok"]
    response = client.get("/admin/heartbeat-logs?page_size=1").json()
    assert response["total"] == 3
    assert len(response["items"]) == 1
    assert not response["items"][0]["ok"]
    assert response["items"][0]["project_code"] == ""
    assert response["summary"] == [
        {"project_code": "test_project", "license_note": "销售一组", "count": 1}
    ]
    filtered = client.get("/admin/heartbeat-logs?device_id=unknown-device").json()
    assert filtered["total"] == 2
    assert filtered["summary"] == []


def test_heartbeat_summary_tracks_current_online_bindings(client):
    import sqlite3

    headers = login(client)
    key = create_license(client, headers, note="实时组")[0]
    payload = {
        "license_key": key,
        "device_id": "realtime-device",
        "project": "test_project",
    }

    assert client.post("/api/verify", json=payload).json()["ok"] is True
    response = client.get("/admin/heartbeat-logs").json()
    assert response["summary"] == [
        {"project_code": "test_project", "license_note": "实时组", "count": 1}
    ]
    assert response["online_device_count"] == 1
    assert response["online_timeout_seconds"] == api.ONLINE_TIMEOUT

    connection = sqlite3.connect(client.test_db_path)
    try:
        connection.execute(
            "UPDATE device_bindings SET last_heartbeat = ? WHERE device_id = ?",
            ("2000-01-01 00:00:00", "realtime-device"),
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get("/admin/heartbeat-logs").json()
    assert response["summary"] == []
    assert response["online_device_count"] == 0

    assert client.post("/api/verify", json=payload).json()["ok"] is True
    response = client.get("/admin/heartbeat-logs").json()
    assert response["summary"][0]["count"] == 1
    assert response["online_device_count"] == 1


def test_admin_cookie_csrf_and_validation(client):
    assert client.get("/admin/projects").status_code == 401
    headers = login(client)
    assert client.get("/admin/projects").status_code == 200
    assert (
        client.post(
            "/admin/projects", json={"code": "sample", "name": "Sample"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/projects",
            json={"code": "bad'code", "name": "Sample"},
            headers=headers,
        ).status_code
        == 422
    )
    keys = create_license(client, headers, count=25)
    assert len(keys) == 25
    assert client.get("/admin/stats").json()["total_licenses"] == 25
    assert client.get("/admin/licenses").json()["total"] == 25
    listing = client.get("/admin/licenses").json()["licenses"]
    assert listing[0]["is_legacy"] is False
    assert listing[0]["key"] != keys[-1]
    assert (
        client.post(
            "/admin/licenses",
            json={"project_code": "test_project", "count": 101},
            headers=headers,
        ).status_code
        == 422
    )
    client.post("/admin/logout")
    assert client.get("/admin/projects").status_code == 401


def test_hashed_admin_password_replaces_login_key(client, monkeypatch):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", b"test-password-example", salt, 600_000)
    monkeypatch.setattr(
        api,
        "ADMIN_PASSWORD_HASH",
        f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}",
    )
    api.validate_settings()
    assert (
        client.post(
            "/admin/login",
            json={"admin_key": "test-admin-key-with-at-least-32-chars"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/admin/login", json={"admin_key": "test-password-example"}
        ).status_code
        == 200
    )
    assert client.get("/admin/me").status_code == 200


def test_license_search_status_project_and_pagination(client):
    headers = login(client)
    keys = create_license(client, headers, count=2)
    first = keys[0].split(".")[0]
    second = keys[1].split(".")[0]
    updated = client.post(
        "/admin/update",
        json={"license_key": first, "note": "customer_100%"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    revoked = client.post(
        "/admin/revoke", json={"license_key": second}, headers=headers
    )
    assert revoked.status_code == 200, revoked.text
    other = client.post(
        "/admin/projects",
        json={"code": "other", "name": "Other"},
        headers=headers,
    )
    assert other.status_code == 200
    expired = client.post(
        "/admin/licenses",
        json={"project_code": "other", "expires_days": 1, "note": "expired"},
        headers=headers,
    )
    assert expired.status_code == 200
    import sqlite3

    connection = sqlite3.connect(client.test_db_path)
    try:
        connection.execute(
            "UPDATE licenses SET expires_at = '2020-01-01 00:00:00' "
            "WHERE project_code = 'other'"
        )
        connection.commit()
    finally:
        connection.close()

    def listing(**params):
        response = client.get("/admin/licenses", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    assert [row["key"] for row in listing(search=first)["licenses"]] == [first]
    assert [row["key"] for row in listing(search="customer_100%")["licenses"]] == [
        first
    ]
    assert listing(search="customer_100X")["total"] == 0
    assert (
        listing(search=keys[0])["total"] == 1
    )  # Exact full-card search uses its hash.
    assert listing(status="active")["total"] == 1
    assert listing(status="revoked")["total"] == 1
    assert listing(status="expired")["total"] == 1
    assert listing(project="test_project", status="expired")["total"] == 0
    assert listing(project="test_project", page_size=1)["total_pages"] == 2
    assert len(listing(project="test_project", page_size=1, page=2)["licenses"]) == 1
    assert (
        client.get("/admin/licenses", params={"status": "invalid"}).status_code == 422
    )
    stats = client.get("/admin/stats").json()
    assert stats["total_licenses"] == (
        stats["active_licenses"] + stats["expired_licenses"] + stats["revoked_licenses"]
    )
    projects = client.get("/admin/projects").json()["projects"]
    assert (
        next(p for p in projects if p["code"] == "other")["active_license_count"] == 0
    )
    audit = client.get("/admin/audit", params={"page_size": 1}).json()
    assert audit["total_pages"] == audit["total"]
    assert len(audit["items"]) == 1


def test_license_creation_idempotency_and_stats_breakdown(client):
    headers = login(client)
    project = client.post(
        "/admin/projects",
        json={"code": "safe_delete", "name": "Safe Delete"},
        headers=headers,
    )
    assert project.status_code == 200
    payload = {
        "project_code": "safe_delete",
        "count": 2,
        "request_id": "request_1234567890abcd",
    }
    first = client.post("/admin/licenses", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    duplicate = client.post("/admin/licenses", json=payload, headers=headers)
    assert duplicate.status_code == 409
    assert client.get("/admin/stats").json() == {
        "ok": True,
        "total_licenses": 2,
        "active_licenses": 2,
        "expired_licenses": 0,
        "revoked_licenses": 0,
        "bound_devices": 0,
    }
    revoked_key = first.json()["licenses"][0].split(".")[0]
    assert (
        client.post(
            "/admin/revoke",
            json={"license_key": revoked_key},
            headers=headers,
        ).status_code
        == 200
    )
    stats = client.get("/admin/stats").json()
    assert stats["active_licenses"] == 1
    assert stats["revoked_licenses"] == 1


def test_project_delete_requires_matching_license_count(client):
    headers = login(client)
    client.post(
        "/admin/projects",
        json={"code": "delete_me", "name": "Delete Me"},
        headers=headers,
    )
    created = client.post(
        "/admin/licenses",
        json={"project_code": "delete_me"},
        headers=headers,
    )
    assert created.status_code == 200
    assert (
        client.delete("/admin/projects/delete_me", headers=headers).status_code == 422
    )
    response = client.delete(
        "/admin/projects/delete_me?expected_count=0",
        headers=headers,
    )
    assert response.status_code == 409
    assert client.get("/admin/licenses").json()["total"] == 1
    response = client.delete(
        "/admin/projects/delete_me?expected_count=1",
        headers=headers,
    )
    assert response.status_code == 200
    assert client.get("/admin/licenses").json()["total"] == 0


def test_verify_signed_binding_and_revocation(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    identifier = key.split(".")[0]
    import sqlite3

    connection = sqlite3.connect(client.test_db_path)
    try:
        row = connection.execute(
            "SELECT key, key_hash FROM licenses WHERE key=?", (identifier,)
        ).fetchone()
    finally:
        connection.close()
    assert row[0] == identifier
    assert key not in row
    assert len(row[1]) == 64
    request = {
        "license_key": key,
        "device_id": "device-a",
        "project": "test_project",
        "request_id": "nonce-1",
    }
    assert (
        client.post(
            "/api/verify",
            json={**request, "license_key": identifier + "." + "0" * 32},
        ).json()["ok"]
        is False
    )
    response = client.post("/api/verify", json=request).json()
    assert response["ok"] is True
    assert response["request_id"] == "nonce-1"
    signature = base64.b64decode(response["signature"])
    payload = {k: v for k, v in response.items() if k != "signature"}
    public_key = serialization.load_pem_public_key(
        client.get("/api/public-key").content
    )
    public_key.verify(
        signature,
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    verified = verify_response(
        response,
        client.get("/api/public-key").content,
        request_id="nonce-1",
        device_id="device-a",
        expected_project="test_project",
    )
    assert verified["ok"] is True
    with pytest.raises(InvalidLicenseResponse):
        verify_response(
            {**response, "max_devices": 999},
            client.get("/api/public-key").content,
            request_id="nonce-1",
            device_id="device-a",
        )
    assert (
        client.post("/api/verify", json={**request, "device_id": "device-b"}).json()[
            "ok"
        ]
        is False
    )
    assert client.post("/api/heartbeat", json=request).json()["ok"] is True
    devices = client.get(f"/admin/devices/{identifier}").json()["devices"]
    assert len(devices) == 1
    assert devices[0]["last_ip"]
    heartbeat_logs = client.get("/admin/heartbeat-logs?page_size=20").json()
    assert heartbeat_logs["ok"] is True
    assert heartbeat_logs["total"] >= 2
    assert {item["event_type"] for item in heartbeat_logs["items"]} >= {
        "verify",
        "heartbeat",
    }
    assert all(item["license_key"] == identifier for item in heartbeat_logs["items"])
    assert (
        client.post(
            "/admin/revoke", json={"license_key": identifier}, headers=headers
        ).status_code
        == 200
    )
    assert client.post("/api/heartbeat", json=request).json()["ok"] is False
    assert client.get("/admin/audit").json()["total"] >= 3


def test_first_activation_and_project_stop(client):
    headers = login(client)
    client.post(
        "/admin/projects",
        json={"code": "activation", "name": "Activation"},
        headers=headers,
    )
    response = client.post(
        "/admin/licenses",
        json={
            "project_code": "activation",
            "expires_days": 7,
            "starts_on_first_activation": True,
        },
        headers=headers,
    )
    key = response.json()["licenses"][0]
    listing = client.get("/admin/licenses", headers=headers).json()["licenses"][0]
    assert listing["expires_at"] is None
    assert listing["duration_days"] == 7
    payload = {"license_key": key, "device_id": "device-one", "project": "activation"}
    assert client.post("/api/verify", json=payload).json()["ok"] is True
    listing = client.get("/admin/licenses", headers=headers).json()["licenses"][0]
    assert listing["activated_at"] is not None
    assert listing["expires_at"] is not None
    client.put(
        "/admin/projects/activation",
        json={"is_active": False},
        headers=headers,
    )
    assert client.post("/api/heartbeat", json=payload).json()["ok"] is False


def test_device_info_is_untrusted_and_concurrent_limit(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    identifier = key.split(".")[0]
    payload = {"license_key": key, "project": "test_project"}

    def verify(i):
        return client.post(
            "/api/verify", json={**payload, "device_id": f"device-{i}"}
        ).json()["ok"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(verify, range(6)))
    assert results.count(True) == 1
    assert len(client.get(f"/admin/devices/{identifier}").json()["devices"]) == 1
