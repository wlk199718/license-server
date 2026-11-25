import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

import db


class ProjectTeamMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_adds_team_without_replacing_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "license.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(64) NOT NULL UNIQUE,
                    name VARCHAR(128) NOT NULL,
                    description VARCHAR(512),
                    is_active BOOLEAN,
                    created_at DATETIME
                )
                """
            )
            connection.execute(
                "INSERT INTO projects "
                "(code, name, description, is_active) VALUES (?, ?, ?, ?)",
                ("existing-project", "Existing Project", "keep me", 1),
            )
            connection.commit()
            connection.close()

            previous_engine = db.engine
            test_engine = create_async_engine(
                f"sqlite+aiosqlite:///{database_path.as_posix()}"
            )
            db.engine = test_engine
            try:
                await db.init_db()
            finally:
                db.engine = previous_engine
                await test_engine.dispose()

            connection = sqlite3.connect(database_path)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(projects)")
            }
            row = connection.execute(
                "SELECT code, name, description, team FROM projects"
            ).fetchone()
            connection.close()

            self.assertIn("team", columns)
            self.assertEqual(
                row,
                ("existing-project", "Existing Project", "keep me", ""),
            )

    async def test_legacy_licenses_and_duplicate_bindings_migrate(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "license.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    code VARCHAR(64) UNIQUE NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    description VARCHAR(512),
                    is_active BOOLEAN,
                    created_at DATETIME
                );
                CREATE TABLE licenses (
                    id INTEGER PRIMARY KEY,
                    key VARCHAR(64) UNIQUE NOT NULL,
                    project_code VARCHAR(64) NOT NULL,
                    max_devices INTEGER,
                    expires_at DATETIME,
                    is_active BOOLEAN,
                    created_at DATETIME,
                    note VARCHAR(256)
                );
                CREATE TABLE device_bindings (
                    id INTEGER PRIMARY KEY,
                    license_key VARCHAR(64) NOT NULL,
                    device_id VARCHAR(128) NOT NULL,
                    device_info TEXT,
                    first_seen DATETIME,
                    last_heartbeat DATETIME,
                    is_online BOOLEAN
                );
                INSERT INTO projects (code, name) VALUES ('legacy', 'Legacy');
                INSERT INTO licenses (key, project_code, max_devices)
                VALUES ('legacy-plaintext-key', 'legacy', 1);
                INSERT INTO device_bindings (id, license_key, device_id, last_heartbeat)
                VALUES
                  (1, 'legacy-plaintext-key', 'device-a', '2026-01-01'),
                  (2, 'legacy-plaintext-key', 'device-a', '2026-02-01');
                """
            )
            connection.close()

            previous_engine = db.engine
            test_engine = create_async_engine(
                f"sqlite+aiosqlite:///{database_path.as_posix()}"
            )
            db.engine = test_engine
            try:
                await db.init_db()
                await db.init_db()
            finally:
                db.engine = previous_engine
                await test_engine.dispose()

            connection = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(licenses)")
                }
                binding_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM device_bindings ORDER BY id"
                    )
                ]
                self.assertTrue(
                    {"key_hash", "activated_at", "duration_days"} <= columns
                )
                binding_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(device_bindings)")
                }
                self.assertIn("last_ip", binding_columns)
                self.assertEqual(binding_ids, [2])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO device_bindings (license_key, device_id) "
                        "VALUES ('legacy-plaintext-key', 'device-a')"
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
