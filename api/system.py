from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from api.support import require_admin, require_identity
from services.config import config
from services.system_maintenance_service import system_maintenance_service


class AuthKeyRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_key: str
    new_key: str
    confirm_key: str


class SystemSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int | None = None
    memory_warning_percent: int | None = None
    memory_baseline_mb: int | None = None
    memory_per_worker_mb: int | None = None


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[str]
    dry_run: bool = False


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool


def create_router(app_version: str) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/login")
    async def login(authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        return {
            "ok": True,
            "version": app_version,
            "role": identity.get("role"),
            "subject_id": identity.get("id"),
            "name": identity.get("name"),
        }

    @router.get("/version")
    async def get_version():
        return {"version": app_version, "edition": "register-only"}

    @router.get("/api/system/overview")
    async def get_overview(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "app": {"name": "摸鱼机", "version": app_version},
            "settings": system_maintenance_service.get_settings(),
            "auth": {"source": config.auth_key_source, "rotatable": config.auth_key_source == "config"},
            "memory": system_maintenance_service.memory_snapshot(),
            "cleanup": system_maintenance_service.cleanup_status(),
            "git": system_maintenance_service.git_status(),
            "restart": system_maintenance_service.restart_status(),
            "hot_reload": True,
        }

    @router.get("/api/system/metrics")
    async def get_metrics(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"memory": system_maintenance_service.memory_snapshot()}

    @router.patch("/api/system/settings")
    async def update_settings(payload: SystemSettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"settings": system_maintenance_service.update_settings(payload.model_dump(exclude_none=True))}

    @router.post("/api/system/auth-key")
    async def rotate_auth_key(payload: AuthKeyRotateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        current_key = payload.current_key.strip()
        new_key = payload.new_key.strip()
        if new_key != payload.confirm_key.strip():
            raise HTTPException(status_code=400, detail="The new admin key confirmation does not match")
        if len(new_key) < 6 or len(new_key) > 256:
            raise HTTPException(status_code=400, detail="The new admin key must be 6 to 256 characters")
        if new_key == current_key:
            raise HTTPException(status_code=400, detail="The new admin key must differ from the current key")
        try:
            config.update_auth_key(current_key, new_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "auth": {"source": config.auth_key_source, "rotatable": True}}

    @router.post("/api/system/cleanup")
    def cleanup(payload: CleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return system_maintenance_service.cleanup(payload.categories, payload.dry_run)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/system/reset")
    def reset_system(payload: ResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not payload.confirm:
            raise HTTPException(status_code=400, detail="Reset confirmation is required")
        return {
            "ok": True,
            "settings": system_maintenance_service.reset_settings(),
            "preserved": ["admin_key", "account_inventory", "mailbox_inventory", "proxy_inventory"],
        }

    @router.get("/api/system/git/status")
    async def get_git_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"git": system_maintenance_service.git_status()}

    @router.post("/api/system/git/check")
    def check_git(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"git": system_maintenance_service.git_check()}
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/system/git/update")
    def update_git(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return system_maintenance_service.git_update()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/system/restart")
    def restart_project(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return system_maintenance_service.schedule_restart()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
