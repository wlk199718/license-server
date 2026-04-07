"""
卡密验证服务端 API
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db import Project, License, DeviceBinding, get_db

router = APIRouter()

ADMIN_KEY = os.getenv("ADMIN_KEY", "")
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "120"))


# ==================== 请求/响应模型 ====================


class VerifyRequest(BaseModel):
    license_key: str
    device_id: str
    device_info: str = ""
    project: str = ""  # 项目标识，可选


class HeartbeatRequest(BaseModel):
    license_key: str
    device_id: str


class CreateProjectRequest(BaseModel):
    code: str
    name: str
    description: str = ""


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CreateLicenseRequest(BaseModel):
    project_code: str
    note: str = ""
    max_devices: int = 1
    expires_days: Optional[int] = None
    count: int = 1


class RevokeLicenseRequest(BaseModel):
    license_key: str


class ActivateLicenseRequest(BaseModel):
    license_key: str


class DeleteLicenseRequest(BaseModel):
    license_key: str


class UpdateLicenseRequest(BaseModel):
    license_key: str
    note: Optional[str] = None
    max_devices: Optional[int] = None


class UnbindDeviceRequest(BaseModel):
    license_key: str
    device_id: str


# ==================== 管理员鉴权 ====================


async def verify_admin(x_admin_key: str = Header(...)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")


# ==================== 客户端接口 ====================


@router.post("/api/verify")
async def verify_license(req: VerifyRequest, db: AsyncSession = Depends(get_db)):
    """客户端启动时验证卡密"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        return {"ok": False, "error": "卡密不存在"}
    if not lic.is_active:
        return {"ok": False, "error": "卡密已被吊销"}
    if lic.expires_at and datetime.now(timezone.utc) > lic.expires_at:
        return {"ok": False, "error": "卡密已过期"}

    # 校验项目
    if req.project and lic.project_code != req.project:
        return {"ok": False, "error": "卡密与项目不匹配"}

    # 校验项目是否启用
    result = await db.execute(select(Project).where(Project.code == lic.project_code))
    proj = result.scalar_one_or_none()
    if proj and not proj.is_active:
        return {"ok": False, "error": "该项目已停用"}

    # 清理超时离线设备
    timeout_threshold = datetime.now(timezone.utc) - timedelta(
        seconds=HEARTBEAT_TIMEOUT
    )
    await db.execute(
        delete(DeviceBinding).where(
            DeviceBinding.license_key == req.license_key,
            DeviceBinding.last_heartbeat < timeout_threshold,
        )
    )
    await db.flush()

    # 检查设备绑定
    result = await db.execute(
        select(DeviceBinding).where(
            DeviceBinding.license_key == req.license_key,
            DeviceBinding.device_id == req.device_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.last_heartbeat = datetime.now(timezone.utc)
        existing.is_online = True
        existing.device_info = req.device_info or existing.device_info
    else:
        result = await db.execute(
            select(func.count()).where(DeviceBinding.license_key == req.license_key)
        )
        current_count = result.scalar()
        if current_count >= lic.max_devices:
            return {
                "ok": False,
                "error": f"设备数已达上限 ({current_count}/{lic.max_devices})",
            }
        binding = DeviceBinding(
            license_key=req.license_key,
            device_id=req.device_id,
            device_info=req.device_info,
        )
        db.add(binding)

    await db.commit()

    return {
        "ok": True,
        "heartbeat_interval": HEARTBEAT_TIMEOUT // 2,
        "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        "max_devices": lic.max_devices,
        "project": lic.project_code,
    }


@router.post("/api/heartbeat")
async def heartbeat(req: HeartbeatRequest, db: AsyncSession = Depends(get_db)):
    """客户端定时心跳"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic or not lic.is_active:
        return {"ok": False, "error": "卡密无效或已吊销"}
    if lic.expires_at and datetime.now(timezone.utc) > lic.expires_at:
        return {"ok": False, "error": "卡密已过期"}

    result = await db.execute(
        select(DeviceBinding).where(
            DeviceBinding.license_key == req.license_key,
            DeviceBinding.device_id == req.device_id,
        )
    )
    binding = result.scalar_one_or_none()
    if not binding:
        return {"ok": False, "error": "设备未注册，请重启程序"}

    binding.last_heartbeat = datetime.now(timezone.utc)
    binding.is_online = True
    await db.commit()

    return {"ok": True}


# ==================== 项目管理接口 ====================


@router.post("/admin/projects", dependencies=[Depends(verify_admin)])
async def create_project(req: CreateProjectRequest, db: AsyncSession = Depends(get_db)):
    """创建项目"""
    result = await db.execute(select(Project).where(Project.code == req.code))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="项目标识已存在")

    proj = Project(code=req.code, name=req.name, description=req.description)
    db.add(proj)
    await db.commit()
    return {"ok": True, "project": {"code": proj.code, "name": proj.name}}


@router.get("/admin/projects", dependencies=[Depends(verify_admin)])
async def list_projects(db: AsyncSession = Depends(get_db)):
    """查询所有项目"""
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()

    items = []
    for p in projects:
        # 统计该项目下的卡密数
        r = await db.execute(select(func.count()).where(License.project_code == p.code))
        license_count = r.scalar()
        r2 = await db.execute(
            select(func.count()).where(
                License.project_code == p.code, License.is_active == True
            )
        )
        active_count = r2.scalar()
        items.append(
            {
                "code": p.code,
                "name": p.name,
                "description": p.description,
                "is_active": p.is_active,
                "license_count": license_count,
                "active_license_count": active_count,
                "created_at": p.created_at.isoformat(),
            }
        )

    return {"ok": True, "projects": items}


@router.put("/admin/projects/{code}", dependencies=[Depends(verify_admin)])
async def update_project(
    code: str, req: UpdateProjectRequest, db: AsyncSession = Depends(get_db)
):
    """更新项目"""
    result = await db.execute(select(Project).where(Project.code == code))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")

    if req.name is not None:
        proj.name = req.name
    if req.description is not None:
        proj.description = req.description
    if req.is_active is not None:
        proj.is_active = req.is_active

    await db.commit()
    return {"ok": True, "message": "项目已更新"}


@router.delete("/admin/projects/{code}", dependencies=[Depends(verify_admin)])
async def delete_project(code: str, db: AsyncSession = Depends(get_db)):
    """删除项目（同时删除该项目下所有卡密和设备绑定）"""
    result = await db.execute(select(Project).where(Project.code == code))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 删除该项目下所有设备绑定
    result = await db.execute(select(License.key).where(License.project_code == code))
    keys = [r[0] for r in result.all()]
    if keys:
        await db.execute(
            delete(DeviceBinding).where(DeviceBinding.license_key.in_(keys))
        )
        await db.execute(delete(License).where(License.project_code == code))

    await db.execute(delete(Project).where(Project.code == code))
    await db.commit()
    return {"ok": True, "message": "项目已删除"}


# ==================== 卡密管理接口 ====================


@router.post("/admin/licenses", dependencies=[Depends(verify_admin)])
async def create_licenses(
    req: CreateLicenseRequest, db: AsyncSession = Depends(get_db)
):
    """批量生成卡密"""
    # 校验项目存在
    result = await db.execute(select(Project).where(Project.code == req.project_code))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="项目不存在，请先创建项目")

    keys = []
    expires_at = None
    if req.expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=req.expires_days)

    for _ in range(min(req.count, 100)):
        key = secrets.token_hex(16)
        lic = License(
            key=key,
            project_code=req.project_code,
            note=req.note,
            max_devices=req.max_devices,
            expires_at=expires_at,
        )
        db.add(lic)
        keys.append(key)

    await db.commit()
    return {
        "ok": True,
        "licenses": keys,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "max_devices": req.max_devices,
    }


@router.get("/admin/licenses", dependencies=[Depends(verify_admin)])
async def list_licenses(
    project: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """查询卡密，可按项目筛选，支持分页"""
    base_query = select(License).order_by(License.created_at.desc())
    count_query = select(func.count()).select_from(License)
    if project:
        base_query = base_query.where(License.project_code == project)
        count_query = count_query.where(License.project_code == project)

    # 总数
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # 分页
    offset = (page - 1) * page_size
    query = base_query.offset(offset).limit(page_size)
    result = await db.execute(query)
    licenses = result.scalars().all()

    items = []
    for lic in licenses:
        r = await db.execute(
            select(func.count()).where(DeviceBinding.license_key == lic.key)
        )
        device_count = r.scalar()
        items.append(
            {
                "key": lic.key,
                "project_code": lic.project_code,
                "note": lic.note,
                "max_devices": lic.max_devices,
                "current_devices": device_count,
                "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
                "is_active": lic.is_active,
                "created_at": lic.created_at.isoformat(),
            }
        )

    return {
        "ok": True,
        "licenses": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.post("/admin/revoke", dependencies=[Depends(verify_admin)])
async def revoke_license(req: RevokeLicenseRequest, db: AsyncSession = Depends(get_db)):
    """吊销卡密"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")

    lic.is_active = False
    await db.execute(
        delete(DeviceBinding).where(DeviceBinding.license_key == req.license_key)
    )
    await db.commit()
    return {"ok": True, "message": "卡密已吊销"}


@router.post("/admin/activate", dependencies=[Depends(verify_admin)])
async def activate_license(
    req: ActivateLicenseRequest, db: AsyncSession = Depends(get_db)
):
    """重新启用卡密"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")

    lic.is_active = True
    await db.commit()
    return {"ok": True, "message": "卡密已启用"}


@router.post("/admin/delete", dependencies=[Depends(verify_admin)])
async def delete_license(req: DeleteLicenseRequest, db: AsyncSession = Depends(get_db)):
    """彻底删除卡密"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")

    await db.execute(
        delete(DeviceBinding).where(DeviceBinding.license_key == req.license_key)
    )
    await db.execute(delete(License).where(License.key == req.license_key))
    await db.commit()
    return {"ok": True, "message": "卡密已删除"}


@router.post("/admin/update", dependencies=[Depends(verify_admin)])
async def update_license(req: UpdateLicenseRequest, db: AsyncSession = Depends(get_db)):
    """更新卡密信息"""
    result = await db.execute(select(License).where(License.key == req.license_key))
    lic = result.scalar_one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="卡密不存在")

    if req.note is not None:
        lic.note = req.note
    if req.max_devices is not None:
        lic.max_devices = req.max_devices

    await db.commit()
    return {"ok": True, "message": "卡密已更新"}


@router.post("/admin/unbind", dependencies=[Depends(verify_admin)])
async def unbind_device(req: UnbindDeviceRequest, db: AsyncSession = Depends(get_db)):
    """解绑指定设备"""
    result = await db.execute(
        delete(DeviceBinding).where(
            DeviceBinding.license_key == req.license_key,
            DeviceBinding.device_id == req.device_id,
        )
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="未找到该设备绑定")
    return {"ok": True, "message": "设备已解绑"}


@router.get("/admin/devices/{license_key}", dependencies=[Depends(verify_admin)])
async def list_devices(license_key: str, db: AsyncSession = Depends(get_db)):
    """查看卡密绑定的设备"""
    result = await db.execute(
        select(DeviceBinding).where(DeviceBinding.license_key == license_key)
    )
    devices = result.scalars().all()

    timeout_threshold = datetime.now(timezone.utc) - timedelta(
        seconds=HEARTBEAT_TIMEOUT
    )
    items = []
    for d in devices:
        items.append(
            {
                "device_id": d.device_id,
                "device_info": d.device_info,
                "first_seen": d.first_seen.isoformat(),
                "last_heartbeat": d.last_heartbeat.isoformat(),
                "is_online": d.last_heartbeat > timeout_threshold,
            }
        )

    return {"ok": True, "devices": items}
