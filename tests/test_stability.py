import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

import api
import main
from request_logging import RequestTimingMiddleware
from test_api import client as shared_client, create_license, login

client = shared_client


@pytest.fixture
def settings(monkeypatch):
    for module in (api, main):
        monkeypatch.setattr(module, "ONLINE_TIMEOUT", 180)
        monkeypatch.setattr(module, "DEVICE_BINDING_TTL", 900)
    monkeypatch.setattr(api, "HEARTBEAT_INTERVAL", 60)
    return datetime.now(timezone.utc)


def bind(client, *, max_devices=1):
    headers = login(client)
    key = create_license(client, headers, max_devices=max_devices)[0]
    request = {"license_key": key, "device_id": "device-a", "project": "test_project"}
    assert client.post("/api/verify", json=request).json()["ok"]
    return headers, key, request


def set_age(client, key, now, seconds):
    value = (now - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with closing(sqlite3.connect(client.test_db_path)) as connection, connection:
        connection.execute(
            "UPDATE device_bindings SET last_heartbeat=? WHERE license_key=?",
            (value, key.split(".")[0]),
        )


def test_interval_independent_of_binding_retention(client, settings, monkeypatch):
    _, key, request = bind(client)
    monkeypatch.setattr(api, "DEVICE_BINDING_TTL", 3600)
    payload = client.post("/api/verify", json=request).json()
    assert payload["heartbeat_interval"] == 60
    assert payload["device_id"] == "device-a"
    assert "request_id" in payload and payload["request_id"] is None


def test_offline_device_keeps_reservation_and_can_resume(client, settings):
    _, key, request = bind(client)
    set_age(client, key, settings, 240)
    identifier = key.split(".")[0]
    devices = client.get(f"/admin/devices/{identifier}").json()["devices"]
    assert len(devices) == 1 and devices[0]["is_online"] is False
    assert client.get("/admin/stats").json()["bound_devices"] == 0
    assert client.get("/admin/licenses").json()["licenses"][0]["current_devices"] == 0
    assert client.post("/api/heartbeat", json=request).json()["ok"] is True
    assert client.get(f"/admin/devices/{identifier}").json()["devices"][0]["is_online"]


def test_offline_binding_still_enforces_device_limit(client, settings):
    _, key, request = bind(client)
    set_age(client, key, settings, 240)
    denied = client.post(
        "/api/verify", json={**request, "device_id": "device-b"}
    ).json()
    assert denied["ok"] is False and "设备数已达上限" in denied["error"]
    assert client.post("/api/verify", json=request).json()["ok"] is True


def test_expired_reservation_releases_slot_without_cleanup_tick(client, settings):
    _, key, request = bind(client)
    set_age(client, key, settings, 901)
    payload = client.post(
        "/api/verify", json={**request, "device_id": "device-b"}
    ).json()
    assert payload["ok"] is True
    devices = client.get(f"/admin/devices/{key.split('.')[0]}").json()["devices"]
    assert [item["device_id"] for item in devices] == ["device-b"]


def test_expired_heartbeat_keeps_legacy_error_and_allows_normal_reverify(
    client, settings
):
    _, key, request = bind(client)
    set_age(client, key, settings, 901)
    payload = client.post("/api/heartbeat", json=request).json()
    assert payload["ok"] is False
    assert payload["error"] == "设备未注册，请重新启动程序"
    assert payload["signature"]
    assert client.post("/api/verify", json=request).json()["ok"] is True


@pytest.mark.parametrize("reason", ["revoke", "expire", "stop-project"])
def test_reservation_does_not_override_license_denials(client, settings, reason):
    headers, key, request = bind(client)
    set_age(client, key, settings, 240)
    identifier = key.split(".")[0]
    if reason == "revoke":
        client.post("/admin/revoke", json={"license_key": identifier}, headers=headers)
    elif reason == "stop-project":
        client.put(
            "/admin/projects/test_project", json={"is_active": False}, headers=headers
        )
    else:
        with closing(sqlite3.connect(client.test_db_path)) as connection, connection:
            connection.execute(
                "UPDATE licenses SET expires_at=? WHERE key=?",
                (
                    (settings - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                    identifier,
                ),
            )
    assert client.post("/api/heartbeat", json=request).json()["ok"] is False
    assert client.post("/api/verify", json=request).json()["ok"] is False


def test_cleanup_marks_offline_then_deletes_only_expired_binding(client, settings):
    _, key, _ = bind(client)
    set_age(client, key, settings, 240)
    maker = async_sessionmaker(main.engine, expire_on_commit=False)
    asyncio.run(main._cleanup_once(maker, now=settings))
    with closing(sqlite3.connect(client.test_db_path)) as connection, connection:
        assert connection.execute(
            "SELECT is_online FROM device_bindings"
        ).fetchall() == [(0,)]
    asyncio.run(main._cleanup_once(maker, now=settings + timedelta(seconds=661)))
    with closing(sqlite3.connect(client.test_db_path)) as connection, connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM device_bindings").fetchone()[0]
            == 0
        )


def test_one_day_retention_preserves_recent_logs_and_license(
    client, settings, monkeypatch
):
    _, key, _ = bind(client)
    monkeypatch.setattr(main, "HEARTBEAT_LOG_RETENTION_DAYS", 1)
    cutoff = settings - timedelta(hours=24)
    dates = [cutoff - timedelta(seconds=1), cutoff, cutoff + timedelta(seconds=1)]
    with closing(sqlite3.connect(client.test_db_path)) as connection, connection:
        for number, date in enumerate(dates):
            connection.execute(
                """INSERT INTO heartbeat_logs
                   (license_key, device_id, event_type, ok, created_at)
                   VALUES (?, ?, 'heartbeat', 1, ?)""",
                (
                    key.split(".")[0],
                    f"retention-{number}",
                    date.strftime("%Y-%m-%d %H:%M:%S.%f"),
                ),
            )
    maker = async_sessionmaker(main.engine, expire_on_commit=False)
    asyncio.run(main._cleanup_once(maker, now=settings))
    with closing(sqlite3.connect(client.test_db_path)) as connection:
        assert connection.execute(
            "SELECT device_id FROM heartbeat_logs WHERE device_id LIKE 'retention-%' ORDER BY device_id"
        ).fetchall() == [("retention-1",), ("retention-2",)]
        assert connection.execute("SELECT COUNT(*) FROM licenses").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_manual_unbind_frees_reserved_slot_immediately(client, settings):
    headers, key, request = bind(client)
    set_age(client, key, settings, 240)
    response = client.post(
        "/admin/unbind",
        json={"license_key": key.split(".")[0], "device_id": "device-a"},
        headers=headers,
    )
    assert response.status_code == 200
    assert client.post("/api/verify", json={**request, "device_id": "device-b"}).json()[
        "ok"
    ]


def test_valid_devices_on_shared_ip_have_separate_budgets(client, monkeypatch):
    _, _, request = bind(client, max_devices=2)
    monkeypatch.setattr(api, "VERIFY_DEVICE_LIMIT", 2)
    # bind() has already consumed one request for device-a.
    assert client.post("/api/verify", json=request).status_code == 200
    denied = client.post("/api/verify", json=request)
    assert denied.status_code == 429 and int(denied.headers["Retry-After"]) >= 1
    assert client.post("/api/verify", json={**request, "device_id": "device-b"}).json()[
        "ok"
    ]


def test_device_budget_is_not_reset_by_switching_ip(client, monkeypatch):
    _, _, request = bind(client)
    monkeypatch.setattr(api, "VERIFY_DEVICE_LIMIT", 1)
    monkeypatch.setattr(api, "_client_ip", lambda request: "198.51.100.99")
    assert client.post("/api/verify", json=request).status_code == 429
    assert request["license_key"] not in repr(api._RATE_BUCKETS)
    assert request["device_id"] not in repr(api._RATE_BUCKETS)


def test_coarse_ip_budget_is_still_enforced(client, monkeypatch):
    _, _, request = bind(client, max_devices=3)
    monkeypatch.setattr(api, "VERIFY_IP_LIMIT", 2)
    assert (
        client.post(
            "/api/verify", json={**request, "device_id": "device-b"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/verify", json={**request, "device_id": "device-c"}
        ).status_code
        == 429
    )


def test_heartbeat_device_budget_does_not_affect_peer(client, monkeypatch):
    _, _, request = bind(client, max_devices=2)
    other = {**request, "device_id": "device-b"}
    assert client.post("/api/verify", json=other).json()["ok"]
    monkeypatch.setattr(api, "HEARTBEAT_DEVICE_LIMIT", 1)
    assert client.post("/api/heartbeat", json=request).json()["ok"]
    assert client.post("/api/heartbeat", json=request).status_code == 429
    assert client.post("/api/heartbeat", json=other).json()["ok"]


def test_invalid_cards_share_budget_across_endpoints_but_not_valid_devices(
    client, monkeypatch
):
    _, _, request = bind(client)
    monkeypatch.setattr(api, "INVALID_LICENSE_IP_LIMIT", 1)
    invalid = {**request, "license_key": "INVALID-CARD-SECRET"}
    assert client.post("/api/verify", json=invalid).json()["ok"] is False
    assert client.post("/api/heartbeat", json=invalid).status_code == 429
    assert client.post("/api/heartbeat", json=request).json()["ok"]
    assert not any("INVALID-CARD" in key for key in api._RATE_BUCKETS)
    assert sum("-device:" in key for key in api._RATE_BUCKETS) == 2


def test_limiter_preserves_longer_login_window(monkeypatch):
    monkeypatch.setattr(
        api,
        "_RATE_BUCKETS",
        {
            "admin-login:peer": deque([900.0]),
            **{f"expired:{i}": deque([800.0]) for i in range(10_000)},
        },
    )
    monkeypatch.setattr(
        api,
        "_RATE_WINDOWS",
        {
            "admin-login:peer": 300,
            **{f"expired:{i}": 60 for i in range(10_000)},
        },
    )
    monkeypatch.setattr(api.time, "monotonic", lambda: 1000.0)
    api._rate_limit("heartbeat:new", 20, 60)
    assert "admin-login:peer" in api._RATE_BUCKETS
    assert len(api._RATE_BUCKETS) == 2
    with pytest.raises(HTTPException) as exc:
        api._rate_limit("admin-login:peer", 1, 300)
    assert exc.value.status_code == 429


def test_limiter_memory_is_bounded_and_does_not_evict_live_bucket(monkeypatch):
    monkeypatch.setattr(api, "_RATE_BUCKETS", {})
    monkeypatch.setattr(api, "_RATE_WINDOWS", {})
    monkeypatch.setattr(api, "_RATE_MAX_BUCKETS", 2)
    api._rate_limit("a", 10, 60)
    api._rate_limit("b", 10, 60)
    with pytest.raises(HTTPException):
        api._rate_limit("c", 10, 60)
    assert set(api._RATE_BUCKETS) == {"a", "b"}
    api._rate_limit("a", 10, 60)


def test_limiter_handles_concurrent_callers_atomically(monkeypatch):
    monkeypatch.setattr(api, "_RATE_BUCKETS", {})
    monkeypatch.setattr(api, "_RATE_WINDOWS", {})

    def attempt(_):
        try:
            api._rate_limit("shared", 3, 60)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(attempt, range(25))) == 3


def test_timing_log_is_correlated_and_does_not_log_secrets(client, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error.license_requests")
    request_id = "a" * 32
    secret = "DO-NOT-LOG-THIS-SECRET"
    response = client.post(
        "/api/verify?token=" + secret,
        headers={"X-Request-ID": request_id, "Authorization": secret},
        json={"license_key": secret, "device_id": secret, "request_id": secret},
    )
    assert response.headers["X-Request-ID"] == request_id
    assert float(response.headers["X-Response-Time-Ms"]) >= 0
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "uvicorn.error.license_requests"
    ]
    assert events[-1]["request_id"] == request_id
    assert events[-1]["route"] == "/api/verify"
    assert events[-1]["status"] == 200
    assert secret not in caplog.text


def test_logs_hide_dynamic_admin_path_and_invalid_header(client, caplog):
    login(client)
    caplog.set_level(logging.INFO, logger="uvicorn.error.license_requests")
    secret = "LEGACY-CARD-SECRET"
    response = client.get("/admin/devices/" + secret, headers={"X-Request-ID": secret})
    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["X-Request-ID"] != secret
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "uvicorn.error.license_requests"
    ]
    assert events[-1]["route"] == "/admin/devices/{license_key}"
    assert secret not in caplog.text


@pytest.mark.parametrize("path", ["/not-a-route/SECRET?key=SECRET", "/api/verify"])
def test_404_and_405_have_safe_timing_logs(client, caplog, path):
    caplog.set_level(logging.INFO, logger="uvicorn.error.license_requests")
    response = client.get(path)
    assert response.status_code in {404, 405}
    assert len(response.headers["X-Request-ID"]) == 32
    assert "SECRET" not in caplog.text


def test_unhandled_exception_is_timed_without_exception_message(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error.license_requests")

    async def failing(scope, receive, send):
        raise RuntimeError("SECRET-QUERY-PARAMETERS")

    async def nothing(*args):
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 123),
    }
    with pytest.raises(RuntimeError):
        asyncio.run(RequestTimingMiddleware(failing)(scope, nothing, nothing))
    assert "SECRET" not in caplog.text
    assert '"status":500' in caplog.text
    assert '"error_type":"RuntimeError"' in caplog.text


@pytest.mark.parametrize("online,ttl", [(59, 900), (180, 179)])
def test_invalid_timing_configuration_rejected(client, monkeypatch, online, ttl):
    monkeypatch.setattr(api, "ONLINE_TIMEOUT", online)
    monkeypatch.setattr(api, "DEVICE_BINDING_TTL", ttl)
    with pytest.raises(RuntimeError, match="ONLINE_TIMEOUT"):
        api.validate_settings()
