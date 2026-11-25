from contextlib import closing
import sqlite3

import pytest
import license_storage
from test_api import client as shared_client, create_license, login

client = shared_client


def test_new_cards_survive_refresh_login_and_cipher_restart(client):
    headers = login(client)
    keys = create_license(client, headers, count=2)

    def listing():
        response = client.get("/admin/licenses")
        assert response.headers["Cache-Control"] == "no-store"
        return {item["full_key"] for item in response.json()["licenses"]}

    assert listing() == set(keys)
    client.post("/admin/logout")
    assert client.get("/admin/licenses").status_code == 401
    login(client)
    license_storage._cipher = None
    license_storage.ensure_display_key(existing_encrypted=True)
    assert listing() == set(keys)
    with closing(sqlite3.connect(client.test_db_path)) as db:
        rows = db.execute("SELECT key,key_hash,encrypted_key FROM licenses").fetchall()
    for row in rows:
        assert row[2] and all(key not in repr(row) for key in keys)
    key = keys[0]
    search = client.get("/admin/licenses", params={"search": key}).json()
    assert search["total"] == 1 and search["licenses"][0]["full_key"] == key
    assert client.post(
        "/api/verify",
        json={
            "license_key": key,
            "device_id": "display-test",
            "project": "test_project",
        },
    ).json()["ok"]


def test_old_hash_only_cards_remain_valid_but_not_recoverable(client):
    key = create_license(client, login(client))[0]
    with closing(sqlite3.connect(client.test_db_path)) as db, db:
        db.execute("UPDATE licenses SET encrypted_key=NULL")
    row = client.get("/admin/licenses").json()["licenses"][0]
    assert row["full_key"] is None and "摘要" in row["display_error"]
    assert client.post(
        "/api/verify",
        json={
            "license_key": key,
            "device_id": "old",
            "project": "test_project",
        },
    ).json()["ok"]


def test_missing_key_not_regenerated_for_encrypted_database(client):
    create_license(client, login(client))
    license_storage.DISPLAY_KEY_PATH.unlink()
    with pytest.raises(RuntimeError, match="禁止重新生成"):
        license_storage.ensure_display_key(existing_encrypted=True)
    assert not license_storage.DISPLAY_KEY_PATH.exists()


def test_tampered_ciphertext_never_displayed(client):
    create_license(client, login(client))
    with closing(sqlite3.connect(client.test_db_path)) as db, db:
        db.execute("UPDATE licenses SET encrypted_key='corrupt'")
    row = client.get("/admin/licenses").json()["licenses"][0]
    assert row["full_key"] is None and "异常" in row["display_error"]


def test_legacy_plaintext_cards_are_displayable(client):
    create_license(client, login(client))
    with closing(sqlite3.connect(client.test_db_path)) as db, db:
        db.execute(
            "INSERT INTO licenses(key,project_code,note) VALUES (?,?,?)",
            ("LEGACY-ADMIN-EXAMPLE", "test_project", "admin"),
        )
    row = next(
        x for x in client.get("/admin/licenses").json()["licenses"] if x["is_legacy"]
    )
    assert row["full_key"] == "LEGACY-ADMIN-EXAMPLE"
