from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

import api
from test_api import client as shared_client, create_license, login

client = shared_client


def bad_secret(license_key, device="bad-device"):
    identifier = license_key.split(".", 1)[0]
    secret = license_key.split(".", 1)[1]
    replacement = "0" * len(secret)
    if replacement == secret:
        replacement = "1" * len(secret)
    return {
        "license_key": f"{identifier}.{replacement}",
        "device_id": device,
        "request_id": "lock-test",
    }


def test_tenth_wrong_secret_locks_only_card_and_unlock_restores_it(client):
    headers = login(client)
    locked_key, other_key = create_license(client, headers, count=2)
    for number in range(9):
        route = "/api/verify" if number % 2 == 0 else "/api/heartbeat"
        response = client.post(route, json=bad_secret(locked_key, f"device-{number}"))
        assert response.status_code == 200
        assert response.json()["ok"] is False

    blocked = client.post(
        "/api/heartbeat", json=bad_secret(locked_key, "another-device")
    )
    assert blocked.status_code == 403
    assert blocked.json()["blocked"]
    assert blocked.json()["request_id"] == "lock-test"
    assert "signature" in blocked.json()

    # Same machine remains usable with a different, unlocked card.
    other = client.post(
        "/api/verify",
        json={"license_key": other_key, "device_id": "another-device"},
    )
    assert other.json()["ok"] is True

    locked = client.get("/admin/heartbeat-logs").json()["blacklist"]
    assert locked == [
        {
            "license_identifier": locked_key.split(".", 1)[0],
            "project_code": "test_project",
            "license_note": "",
            "failed_attempts": 10,
            "blocked_at": locked[0]["blocked_at"],
        }
    ]
    assert (
        client.post(
            "/admin/blacklist/unblock",
            json={"license_identifier": locked_key.split(".", 1)[0]},
        ).status_code
        == 403
    )
    assert client.post(
        "/admin/blacklist/unblock",
        json={"license_identifier": locked_key.split(".", 1)[0]},
        headers=headers,
    ).json()["ok"]
    assert client.post(
        "/api/verify",
        json={"license_key": locked_key, "device_id": "another-device"},
    ).json()["ok"]
    assert client.get("/admin/heartbeat-logs").json()["blacklist"] == []


def test_only_bad_secrets_count_and_success_resets_consecutive_failures(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    for _ in range(9):
        client.post("/api/verify", json=bad_secret(key))
    assert client.post(
        "/api/verify", json={"license_key": key, "device_id": "good-device"}
    ).json()["ok"]
    for _ in range(9):
        client.post("/api/heartbeat", json=bad_secret(key))
    assert client.post(
        "/api/heartbeat", json={"license_key": key, "device_id": "good-device"}
    ).json()["ok"]

    # Ordinary binding errors with the correct key never lock a card or device.
    for _ in range(11):
        response = client.post(
            "/api/heartbeat",
            json={"license_key": key, "device_id": "unregistered-device"},
        )
        assert response.status_code == 200
        assert not response.json()["ok"]
    assert client.get("/admin/heartbeat-logs").json()["blacklist"] == []


def test_legacy_machine_blacklist_is_ignored(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    with closing(sqlite3.connect(client.test_db_path)) as db, db:
        db.execute(
            """INSERT INTO client_access
               (device_id, consecutive_errors, client_ip, last_error, blocked_at, updated_at)
               VALUES ('previously-blocked-device', 10, '', 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
    response = client.post(
        "/api/verify",
        json={"license_key": key, "device_id": "previously-blocked-device"},
    )
    assert response.status_code == 200
    assert response.json()["ok"]
    assert client.get("/admin/heartbeat-logs").json()["blacklist"] == []
    with closing(sqlite3.connect(client.test_db_path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM client_access").fetchone()[0] == 1
        )


def test_locked_card_survives_log_deletion_and_rate_limit_reset(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    for _ in range(10):
        client.post("/api/verify", json=bad_secret(key))
    with closing(sqlite3.connect(client.test_db_path)) as db, db:
        db.execute("DELETE FROM heartbeat_logs")
    api._RATE_BUCKETS.clear()
    api._RATE_WINDOWS.clear()
    assert (
        client.post(
            "/api/verify", json={"license_key": key, "device_id": "new-device"}
        ).status_code
        == 403
    )
    assert client.get("/admin/heartbeat-logs").json()["total"] == 0
    assert len(client.get("/admin/heartbeat-logs").json()["blacklist"]) == 1


def test_concurrent_bad_secrets_do_not_lose_card_streak_updates(client):
    headers = login(client)
    key = create_license(client, headers)[0]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(
                lambda _: client.post("/api/heartbeat", json=bad_secret(key)),
                range(10),
            )
        )
    assert all(result.status_code in (200, 403) for result in results)
    assert any(result.status_code == 403 for result in results)
    assert (
        client.post(
            "/api/verify", json={"license_key": key, "device_id": "valid-device"}
        ).status_code
        == 403
    )
    locked = client.get("/admin/heartbeat-logs").json()["blacklist"]
    assert locked[0]["failed_attempts"] == 10
    assert locked[0]["license_identifier"] == key.split(".", 1)[0]
