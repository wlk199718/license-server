"""
数据库模型和初始化
"""

import os
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/license.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(
        String(64), unique=True, nullable=False, index=True
    )  # 项目标识（英文）
    name = Column(String(128), nullable=False)  # 项目显示名
    description = Column(String(512), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    project_code = Column(String(64), nullable=False, index=True)  # 所属项目
    note = Column(String(256), default="")
    max_devices = Column(Integer, default=1)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class DeviceBinding(Base):
    __tablename__ = "device_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    license_key = Column(String(64), nullable=False, index=True)
    device_id = Column(String(128), nullable=False)
    device_info = Column(Text, default="")
    first_seen = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_heartbeat = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    is_online = Column(Boolean, default=True)


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with async_session() as session:
        yield session
