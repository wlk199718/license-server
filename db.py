"""
数据库模型和初始化
"""

import os
from datetime import datetime, timezone

from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'license.db'}",
)

engine_kwargs = {"echo": False, "pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {
        "check_same_thread": False,
        "timeout": 30,
    }

engine = create_async_engine(DATABASE_URL, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(
        String(64), unique=True, nullable=False, index=True
    )  # 项目标识（英文）
    name = Column(String(128), nullable=False)  # 项目显示名
    team = Column(String(128), nullable=False, default="", server_default="")
    description = Column(String(512), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    key_hash = Column(String(64), nullable=True)
    encrypted_key = Column(Text, nullable=True)
    project_code = Column(
        String(64),
        ForeignKey("projects.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    note = Column(String(256), default="", server_default="")
    max_devices = Column(Integer, default=1, server_default="1", nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    duration_days = Column(Integer, nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="1", nullable=False)
    failed_attempts = Column(Integer, nullable=False, server_default="0")
    locked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )


class LicenseCreateRequest(Base):
    __tablename__ = "license_create_requests"

    request_id = Column(String(64), primary_key=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class DeviceBinding(Base):
    __tablename__ = "device_bindings"
    __table_args__ = (
        UniqueConstraint(
            "license_key",
            "device_id",
            name="uq_device_binding_license_device",
        ),
        Index("ix_device_bindings_last_heartbeat", "last_heartbeat"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    license_key = Column(
        String(64),
        ForeignKey("licenses.key", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id = Column(String(128), nullable=False)
    device_info = Column(Text, default="", server_default="")
    first_seen = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_heartbeat = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    is_online = Column(Boolean, default=True, server_default="1", nullable=False)
    last_ip = Column(String(64), nullable=True)


class HeartbeatLog(Base):
    """客户端验证/心跳记录，供管理员排查在线状态和异常设备。"""

    __tablename__ = "heartbeat_logs"
    __table_args__ = (
        Index("ix_heartbeat_logs_created_at", "created_at"),
        Index(
            "ix_heartbeat_logs_license_device_created",
            "license_key",
            "device_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    license_key = Column(String(64), nullable=False, index=True)
    project_code = Column(String(64), nullable=False, server_default="")
    device_id = Column(String(128), nullable=False, index=True)
    event_type = Column(String(16), nullable=False)
    ok = Column(Boolean, nullable=False, server_default="1")
    client_ip = Column(String(64), nullable=False, server_default="")
    device_info = Column(Text, nullable=False, server_default="")
    error = Column(String(256), nullable=False, server_default="")
    request_id = Column(String(128), nullable=False, server_default="")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class ClientAccess(Base):
    """Legacy device blacklist table retained for database compatibility."""

    __tablename__ = "client_access"

    device_id = Column(String(128), primary_key=True)
    consecutive_errors = Column(Integer, nullable=False, server_default="0")
    client_ip = Column(String(64), nullable=False, server_default="")
    last_error = Column(String(256), nullable=False, server_default="")
    blocked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(64), nullable=False)
    target = Column(String(128), nullable=False)
    actor_ip = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        nullable=False,
    )


async def init_db():
    data_dir = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        project_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("projects")
            }
        )
        if "team" not in project_columns:
            await conn.execute(
                text(
                    "ALTER TABLE projects "
                    "ADD COLUMN team VARCHAR(128) NOT NULL DEFAULT ''"
                )
            )
        license_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("licenses")
            }
        )
        if "key_hash" not in license_columns:
            await conn.execute(
                text("ALTER TABLE licenses ADD COLUMN key_hash VARCHAR(64)")
            )
        if "encrypted_key" not in license_columns:
            await conn.execute(
                text("ALTER TABLE licenses ADD COLUMN encrypted_key TEXT")
            )
        if "duration_days" not in license_columns:
            await conn.execute(
                text("ALTER TABLE licenses ADD COLUMN duration_days INTEGER")
            )
        if "activated_at" not in license_columns:
            await conn.execute(
                text("ALTER TABLE licenses ADD COLUMN activated_at DATETIME")
            )
        if "failed_attempts" not in license_columns:
            await conn.execute(
                text(
                    "ALTER TABLE licenses ADD COLUMN failed_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "locked_at" not in license_columns:
            await conn.execute(
                text("ALTER TABLE licenses ADD COLUMN locked_at DATETIME")
            )
        # Older releases incorrectly blocked whole device IDs. Remove those
        # stale blocks; future lockouts are scoped to a specific license.
        await conn.execute(text("DELETE FROM client_access"))
        heartbeat_log_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("heartbeat_logs")
            }
        )
        if heartbeat_log_columns and "project_code" not in heartbeat_log_columns:
            await conn.execute(
                text(
                    "ALTER TABLE heartbeat_logs "
                    "ADD COLUMN project_code VARCHAR(64) NOT NULL DEFAULT ''"
                )
            )
            # Legacy logs contain only redacted card prefixes. Backfill only
            # when that prefix identifies one card; never guess after deletion
            # or collisions, and never accept the client's claimed project.
            await conn.execute(
                text("""
                UPDATE heartbeat_logs
                SET project_code = (
                    SELECT MIN(licenses.project_code)
                    FROM licenses
                    WHERE substr(licenses.key, 1, 16) = heartbeat_logs.license_key
                )
                WHERE project_code = ''
                  AND error NOT IN ('卡密不存在', '卡密无效或已吊销')
                  AND (
                    SELECT COUNT(*)
                    FROM licenses
                    WHERE substr(licenses.key, 1, 16) = heartbeat_logs.license_key
                  ) = 1
            """)
            )
        binding_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("device_bindings")
            }
        )
        if binding_columns:
            if "last_ip" not in binding_columns:
                await conn.execute(
                    text("ALTER TABLE device_bindings ADD COLUMN last_ip VARCHAR(64)")
                )
            # Preserve the most recently active row when old databases
            # contain duplicate bindings.
            await conn.execute(
                text(
                    """
                    DELETE FROM device_bindings
                    WHERE id NOT IN (
                        SELECT id FROM (
                            SELECT id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY license_key, device_id
                                       ORDER BY last_heartbeat DESC, id DESC
                                   ) AS rn
                            FROM device_bindings
                        ) AS ranked
                        WHERE rn = 1
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_device_binding_license_device
                    ON device_bindings (license_key, device_id)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_device_bindings_last_heartbeat
                    ON device_bindings (last_heartbeat)
                    """
                )
            )


async def get_db():
    async with async_session() as session:
        yield session
