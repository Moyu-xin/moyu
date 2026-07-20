from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 5 * 1024 * 1024

from api.support import require_admin
from services import account_inventory_service, account_oauth_service, cpa_store, garbage_health_service, k12_config_store, mailbox_pool_store, managed_mihomo_service, partial_account_health_service, partial_account_store, plus_link_store, proxy_controller, proxy_pool_store, sub2api_store
from services.k12_inventory_service import k12_inventory_service
from services.register import mail_provider
from services.register_service import register_service
from services.registered_account_store import (
    account_matches_selector,
    account_pool,
    account_pool_page,
    clear_registered_accounts,
    delete_registered_account,
    import_registered_accounts,
    list_registered_accounts,
    update_registered_account,
)


class RegisterConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None
    k12_mode: bool | None = None
    register_mode: str | None = None
    product_mode: str | None = None
    mailbox_group: str | None = None


class OutlookPoolResetRequest(BaseModel):
    scope: str | None = None


class MailboxImportRequest(BaseModel):
    text: str
    note: str | None = None
    provider_type: str | None = None
    group: str | None = None


class MailboxGroupUpdateRequest(BaseModel):
    emails: list[str] | None = None
    group: str
    provider_type: str | None = None


class MailProviderConfigsRequest(BaseModel):
    registration_source: str | None = None
    outlook_token: dict[str, Any] | None = None
    cloudflare_temp_email: dict[str, Any] | None = None
    gmail_oauth: dict[str, Any] | None = None
    ddg_mail: dict[str, Any] | None = None
    duckmail: dict[str, Any] | None = None
    openai_cpa: dict[str, Any] | None = None


class MailboxClearRequest(BaseModel):
    remove_states: bool | None = None


class MailboxCodeRequest(BaseModel):
    email: str
    provider_type: str | None = None
    scan_recent: bool | None = True


class MailboxGroupCreateRequest(BaseModel):
    name: str


class MailboxGroupRenameRequest(BaseModel):
    name: str
    new_name: str


class MailboxGroupDeleteRequest(BaseModel):
    name: str
    move_to: str | None = None


class OutlookMailboxMessagesRequest(BaseModel):
    email: str
    limit: int | None = 20
    mode: str | None = "auto"
    request_timeout: float | None = 30


class OutlookMailboxRefreshRequest(BaseModel):
    emails: list[str]
    mode: str | None = "auto"
    request_timeout: float | None = 30
    concurrency: int | None = 5


class OpenAICPAMailWebhookRequest(BaseModel):
    email: str | None = None
    address: str | None = None
    to: str | None = None
    to_addr: str | None = None
    subject: str | None = None
    sender: str | None = None
    text: str | None = None
    text_content: str | None = None
    html: str | None = None
    html_content: str | None = None
    body: str | None = None
    raw: Any | None = None
    raw_content: str | None = None
    secret: str | None = None


class AccountImportRequest(BaseModel):
    text: str
    auto_health_check: bool | None = None
    drop_banned: bool | None = None
    health_method: str | None = None


class PartialAccountUpdateRequest(BaseModel):
    email: str | None = None
    password: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    session_token: str | None = None
    status: str | None = None
    stage: str | None = None
    reason: str | None = None
    note: str | None = None
    plan_type: str | None = None
    account_type: str | None = None
    mailbox_group: str | None = None
    provider_label: str | None = None
    register_mode: str | None = None


class PartialAccountSelectionRequest(BaseModel):
    selectors: list[str] | None = None


class AccountOAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    selector: str
    redirect_uri: str | None = None


class AccountOAuthCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    callback: str


class AccountSub2ApiImportRequest(BaseModel):
    emails: list[str] | None = None


class AccountCpaImportRequest(BaseModel):
    emails: list[str] | None = None


class AccountHealthRequest(BaseModel):
    emails: list[str] | None = None
    method: str | None = None
    drop_banned: bool | None = None
    refresh_quota: bool | None = None
    model_id: str | None = None
    prompt: str | None = None
    mode: str | None = None
    concurrency: int | None = None
    only_stale: bool | None = None
    stale_after_minutes: int | None = None


class PartialAccountHealthRequest(BaseModel):
    selectors: list[str] | None = None
    method: str | None = None
    refresh_quota: bool | None = None
    concurrency: int | None = None
    mode: str | None = None


class PartialAccountHealthSettingsRequest(BaseModel):
    scheduled_enabled: bool | None = None
    interval_minutes: int | None = None
    refresh_quota: bool | None = None
    method: str | None = None
    concurrency: int | None = None


class AccountInventorySettingsRequest(BaseModel):
    scheduled_enabled: bool | None = None
    interval_minutes: int | None = None
    stale_after_minutes: int | None = None
    method: str | None = None
    refresh_quota_on_health: bool | None = None
    import_auto_health_check: bool | None = None
    import_drop_banned: bool | None = None
    auto_drop_banned: bool | None = None
    model_id: str | None = None
    prompt: str | None = None
    mode: str | None = None
    concurrency: int | None = None


class K12StartRequest(BaseModel):
    emails: list[str] | None = None
    only_unprocessed: bool | None = None
    join_k12: bool | None = None
    import_to_sub2api: bool | None = None
    import_to_cpa: bool | None = None
    health_check: bool | None = None


class K12ConfigRequest(BaseModel):
    enabled: bool | None = None
    team_ids: dict[str, Any] | None = None
    join_route: str | None = None
    accept_invite: bool | None = None
    invite_timeout: int | None = None


class Sub2ApiConfigRequest(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    admin_token: str | None = None
    username: str | None = None
    password: str | None = None
    skip_ssl_verify: bool | None = None
    import_method: str | None = None
    group_ids: list[int] | str | None = None
    concurrency: int | None = None
    priority: int | None = None
    rate_multiplier: float | None = None
    load_factor: int | None = None
    auto_pause_on_expired: bool | None = None
    auto_import: bool | None = None
    scheduled_enabled: bool | None = None
    check_interval_minutes: int | None = None
    test_concurrency: int | None = None
    test_model_id: str | None = None
    test_prompt: str | None = None
    test_mode: str | None = None
    disable_on_failure: bool | None = None
    delete_on_failure: bool | None = None
    notes: str | None = None


class CpaConfigRequest(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    management_key: str | None = None
    skip_ssl_verify: bool | None = None
    auto_import: bool | None = None
    scheduled_enabled: bool | None = None
    check_interval_minutes: int | None = None
    concurrency: int | None = None
    quota_threshold: float | None = None
    disable_on_failure: bool | None = None
    delete_dead_accounts: bool | None = None
    delete_quota_exhausted: bool | None = None
    notes: str | None = None


class ProxyControllerRequest(BaseModel):
    enabled: bool | None = None
    api_base: str | None = None
    secret: str | None = None
    selector: str | None = None
    skip_names: list[str] | str | None = None


class ProxyPoolUpdateRequest(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    strategy: str | None = None
    node_mode: str | None = None
    fallback_proxy: str | None = None
    node_blacklist: list[str] | str | None = None
    fastest_mode: bool | None = None
    max_retry_count: int | None = None
    retry_delay: float | None = None
    mihomo: dict[str, Any] | None = None
    managed_mihomo: dict[str, Any] | None = None
    subscriptions: list[dict[str, Any]] | None = None


class ManagedMihomoRequest(BaseModel):
    threads: int | None = None


class ProxyAssignmentPreviewRequest(BaseModel):
    threads: int | None = None
    tasks: int | None = 5


class ProxySubscriptionImportRequest(BaseModel):
    items: list[dict[str, Any]] | None = None
    text: str | None = None
    name: str | None = None
    url: str | None = None
    fetch: bool | None = True
    allow_private: bool | None = False


class ProxyNodesImportRequest(BaseModel):
    text: str


class ProxyNodeTestRequest(BaseModel):
    node_id: str | None = None
    proxy_url: str | None = None


class Sub2ApiAccountsTestRequest(BaseModel):
    account_ids: list[int | str] | None = None
    group_id: int | str | None = None
    model_id: str | None = None
    prompt: str | None = None
    mode: str | None = None
    concurrency: int | None = None
    apply_policy: bool | None = None


class WarehouseAccountIdsRequest(BaseModel):
    account_ids: list[int | str] | None = None


class WarehouseAccountActionRequest(BaseModel):
    account_ids: list[int | str] | None = None
    names: list[str] | None = None
    action: str


class CpaAccountsTestRequest(BaseModel):
    names: list[str] | None = None
    apply_policy: bool | None = None


class PlusLinkConfigRequest(BaseModel):
    engine_mode: str | None = None
    use_proxy_pool: bool | None = None
    use_residential_proxy_pool: bool | None = None
    residential_proxy_pool: str | None = None
    residential_country_proxy_pool: dict[str, Any] | str | None = None
    residential_checkout_proxy: str | None = None
    residential_provider_proxy: str | None = None
    residential_approve_proxy: str | None = None
    residential_promotion_proxy: str | None = None
    residential_single_proxy_mode: bool | None = None
    residential_session_ttl_minutes: int | None = None
    residential_via_project_proxy: bool | None = None
    residential_parent_node_filter: str | None = None
    residential_via_clash: bool | None = None
    residential_clash_proxy: str | None = None
    proxy: str | None = None
    target_country: str | None = None
    checkout_country: str | None = None
    promotion_country: str | None = None
    payment_method_country: str | None = None
    payment_locale: str | None = None
    concurrency: int | None = None
    timeout_seconds: int | None = None
    checkout_retry_limit: int | None = None
    provider_retry_limit: int | None = None
    matrix_combo_retry_limit: int | None = None
    retry_limits_version: int | None = None
    refresh_tokens: bool | None = None
    provider_same_as_target: bool | None = None
    approve_same_as_checkout: bool | None = None
    country_rotation_enabled: bool | None = None
    billing_country_rotation: list[str] | str | None = None
    proxy_country_rotation: list[str] | str | None = None
    require_zero_stripe_amount: bool | None = None
    strict_same_proxy_ip: bool | None = None
    strict_approve_same_proxy_ip: bool | None = None


class PlusLinkStartRequest(BaseModel):
    emails: list[str] | None = None
    partial_accounts: list[str] | None = None
    manual_accounts: list[dict[str, Any] | str] | None = None
    manual_tokens: list[str] | None = None
    clear_results: bool | None = True
    config: dict[str, Any] | None = None


class GarbageHealthConfigRequest(BaseModel):
    input_dir: str | None = None
    output_dir: str | None = None
    proxy: str | None = None
    concurrency: int | None = None
    timeout: int | None = None
    backend: str | None = None
    refresh_tokens: bool | None = None
    include_archives: bool | None = None
    check_models: bool | None = None
    check_quota: bool | None = None
    keep_alive_only: bool | None = None
    keep_with_quota_only: bool | None = None
    realtime_import_sub2api: bool | None = None
    sub2api_group_id: str | None = None
    auto_delete_dead: bool | None = None
    limit: int | None = None


class GarbageHealthStartRequest(BaseModel):
    clear_results: bool | None = True
    config: dict[str, Any] | None = None


class GarbageHealthImportRequest(BaseModel):
    group_id: int | str | None = None




def create_router() -> APIRouter:
    k12_config_store.ensure_migrated()
    router = APIRouter()

    def proxy_pool_changed(proxy_pool: dict) -> dict:
        return {"proxy_pool": proxy_pool, "hot_reload": register_service.hot_reload_proxy_config()}

    def reject_disruptive_proxy_action() -> None:
        if register_service.is_running():
            raise HTTPException(status_code=409, detail="注册任务运行中，不能启动、停止或切换底层代理进程；可继续保存节点和代理池配置，后续任务会自动使用新配置")

    @router.get("/api/register")
    async def get_register_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.get()}

    @router.post("/api/register")
    async def update_register_config(body: RegisterConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = body.model_dump(exclude_none=True)
        register = register_service.update(updates)
        if "k12_mode" in updates or "register_mode" in updates:
            k12_config_store.update_k12_config({"enabled": bool(register.get("k12_mode"))})
        return {"register": register}

    @router.post("/api/register/start")
    async def start_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"register": register_service.start()}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/register/stop")
    async def stop_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.stop()}

    @router.post("/api/register/reset")
    async def reset_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset()}

    @router.post("/api/register/outlook-pool/reset")
    async def reset_outlook_pool(body: OutlookPoolResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset_outlook_pool(body.scope or "all")}

    @router.get("/api/register/events")
    async def register_events(token: str = ""):
        require_admin(f"Bearer {token}")

        async def stream():
            last = ""
            while True:
                payload = json.dumps(register_service.get(), ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.get("/api/mailboxes")
    async def list_mailboxes(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"mailboxes": mailbox_pool_store.list_mailboxes()}

    @router.get("/api/outlook-mailboxes")
    async def list_outlook_mailboxes(limit: int = Query(default=0, ge=0, le=500), offset: int = Query(default=0, ge=0), authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailboxes = mailbox_pool_store.list_outlook_mailboxes_page(limit, offset) if limit > 0 else mailbox_pool_store.list_outlook_mailboxes()
        return {"mailboxes": mailboxes}

    @router.post("/api/outlook-mailboxes/messages")
    async def get_outlook_mailbox_messages(body: OutlookMailboxMessagesRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = mailbox_pool_store.recent_outlook_messages(
                body.email,
                limit=body.limit or 20,
                mode=body.mode or "auto",
                request_timeout=body.request_timeout or 30,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"mailbox_messages": result}

    @router.post("/api/outlook-mailboxes/refresh-token")
    async def refresh_outlook_mailbox_tokens(body: OutlookMailboxRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return mailbox_pool_store.refresh_outlook_tokens(
                body.emails,
                mode=body.mode or "auto",
                request_timeout=body.request_timeout or 30,
                concurrency=body.concurrency or 5,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/mailboxes/import")
    async def import_mailboxes(body: MailboxImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailboxes = mailbox_pool_store.import_mailbox_text(body.text, provider_type=body.provider_type or "outlook_token", note=body.note or "", group=body.group or "")
        register = register_service.sync_mailbox_pool()
        return {"mailboxes": mailboxes, "register": register}

    @router.post("/api/mailboxes/group")
    async def update_mailbox_groups(body: MailboxGroupUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailboxes = mailbox_pool_store.update_mailbox_groups(body.emails or [], body.group, provider_type=body.provider_type or "")
        register = register_service.sync_mailbox_pool()
        return {"mailboxes": mailboxes, "register": register}

    @router.get("/api/mailbox-groups")
    async def get_mailbox_groups(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"mailbox_groups": mailbox_pool_store.mailbox_group_catalog()}

    @router.post("/api/mailbox-groups")
    async def create_mailbox_group(body: MailboxGroupCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            groups = mailbox_pool_store.create_mailbox_group(body.name)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"mailbox_groups": groups, "register": register_service.sync_mailbox_pool()}

    @router.patch("/api/mailbox-groups")
    async def rename_mailbox_group(body: MailboxGroupRenameRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            groups = mailbox_pool_store.rename_mailbox_group(body.name, body.new_name)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"mailbox_groups": groups, "register": register_service.sync_mailbox_pool()}

    @router.delete("/api/mailbox-groups")
    async def delete_mailbox_group(body: MailboxGroupDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            groups = mailbox_pool_store.delete_mailbox_group(body.name, move_to=body.move_to or mailbox_pool_store.DEFAULT_MAILBOX_GROUP)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"mailbox_groups": groups, "register": register_service.sync_mailbox_pool()}

    @router.post("/api/mailboxes/reset-state")
    async def reset_mailbox_states(body: OutlookPoolResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"mailboxes": mailbox_pool_store.reset_states(body.scope or "failed"), "register": register_service.sync_mailbox_pool()}

    @router.post("/api/mailboxes/clear")
    async def clear_mailboxes(body: MailboxClearRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailboxes = mailbox_pool_store.clear_mailboxes(remove_states=bool(body.remove_states))
        register = register_service.sync_mailbox_pool()
        return {"mailboxes": mailboxes, "register": register}

    @router.post("/api/mailboxes/code")
    async def get_mailbox_code(body: MailboxCodeRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = mailbox_pool_store.latest_verification_code(
                body.email,
                provider_type=body.provider_type or "",
                scan_recent=body.scan_recent is not False,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"mailbox_code": result}

    @router.get("/api/mail-provider-configs")
    async def get_mail_provider_configs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"provider_configs": mailbox_pool_store.get_provider_configs()}

    @router.post("/api/mail-provider-configs")
    async def update_mail_provider_configs(body: MailProviderConfigsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        configs = mailbox_pool_store.update_provider_configs(body.model_dump(exclude_none=True))
        register = register_service.sync_mailbox_pool()
        return {"provider_configs": configs, "mailboxes": mailbox_pool_store.list_mailboxes(), "register": register}

    @router.post("/api/mailboxes/openai-cpa/webhook")
    @router.post("/api/webhook/email")
    async def openai_cpa_mail_webhook(request: Request, x_email_webhook_secret: str | None = Header(default=None), x_webhook_secret: str | None = Header(default=None)):
        content_length = str(request.headers.get("content-length") or "").strip()
        if content_length.isdigit() and int(content_length) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        chunks: list[bytes] = []
        body_length = 0
        async for chunk in request.stream():
            body_length += len(chunk)
            if body_length > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="webhook payload is too large")
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        try:
            parsed = json.loads(raw_body)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            logger.warning("email webhook rejected: invalid json body_len=%s", len(raw_body))
            raise HTTPException(status_code=422, detail="invalid json payload") from error
        configs = mailbox_pool_store.get_provider_configs(include_secret=True)
        cfg = configs.get("openai_cpa") if isinstance(configs.get("openai_cpa"), dict) else {}
        expected = str(cfg.get("webhook_secret") or "").strip()
        provided = str(x_email_webhook_secret or x_webhook_secret or parsed.get("secret") or "").strip()
        if not expected:
            logger.error("email webhook rejected: webhook secret is not configured")
            raise HTTPException(status_code=503, detail="webhook is not configured")
        if not hmac.compare_digest(provided, expected):
            logger.warning("email webhook rejected: invalid secret body_len=%s", len(raw_body))
            raise HTTPException(status_code=403, detail="invalid webhook secret")
        try:
            body = OpenAICPAMailWebhookRequest(**parsed)
        except Exception as error:
            logger.warning("email webhook rejected: invalid schema body_len=%s", len(raw_body))
            raise HTTPException(status_code=422, detail="invalid webhook payload") from error
        payload = body.model_dump(exclude_none=True)
        address = str(body.email or body.address or body.to or body.to_addr or "").strip()
        if not address:
            logger.warning("email webhook payload has no recipient")
        stored = mail_provider.push_openai_cpa_message(address, payload)
        logger.info("email webhook processed: stored=%s recipient_present=%s", stored, bool(address))
        return {"ok": True, "stored": stored}

    @router.delete("/api/mailboxes/{email}")
    async def delete_mailbox(email: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailboxes = mailbox_pool_store.delete_mailbox(unquote(email))
        register = register_service.sync_mailbox_pool()
        return {"mailboxes": mailboxes, "register": register}

    @router.get("/api/accounts")
    async def list_accounts(limit: int = Query(default=0, ge=0, le=500), offset: int = Query(default=0, ge=0), authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = account_pool_page(limit, offset) if limit > 0 else account_pool()
        return {"account_pool": pool}

    @router.get("/api/partial-accounts")
    async def list_partial_accounts(limit: int = Query(default=0, ge=0, le=500), offset: int = Query(default=0, ge=0), authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = partial_account_store.partial_account_pool_page(limit, offset, include_secret=True) if limit > 0 else partial_account_store.partial_account_pool(include_secret=True)
        return {"partial_account_pool": pool}

    @router.post("/api/partial-accounts/import")
    def import_partial_accounts(body: AccountImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = partial_account_store.import_partial_accounts(body.text)
        changed_selectors = pool.get("changed_selectors") or []
        health = None
        if body.auto_health_check and changed_selectors:
            health = partial_account_health_service.check_partial_accounts(
                changed_selectors,
                refresh_quota=True,
                concurrency=min(32, max(1, len(changed_selectors))),
            )
            if body.drop_banned:
                checked = partial_account_store.select_partial_accounts(changed_selectors, include_secret=True)
                terminal = [
                    str(item.get("id") or item.get("email") or "")
                    for item in checked
                    if str(item.get("health_status") or "").strip().lower() in partial_account_store.TERMINAL_HEALTH_STATUSES
                ]
                if terminal:
                    partial_account_store.delete_partial_accounts(terminal)
        return {
            "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
            "partial_health": health,
            "imported": pool.get("imported", 0),
            "updated": pool.get("updated", 0),
            "skipped": pool.get("skipped", 0),
            "parsed": pool.get("parsed", 0),
        }

    @router.post("/api/partial-accounts/delete")
    async def delete_partial_accounts(body: PartialAccountSelectionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_account_pool": partial_account_store.delete_partial_accounts(body.selectors or [])}

    @router.post("/api/partial-accounts/prune-banned")
    async def prune_partial_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_account_pool": partial_account_store.prune_terminal_partial_accounts()}

    @router.post("/api/partial-accounts/export-sub2api")
    async def export_partial_accounts_sub2api(body: PartialAccountSelectionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        accounts = partial_account_store.select_partial_accounts(body.selectors, include_secret=True)
        return {"sub2api": sub2api_store.export_account_dicts_to_sub2api(accounts)}

    @router.post("/api/partial-accounts/import-sub2api")
    def import_partial_accounts_sub2api(body: PartialAccountSelectionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.import_partial_accounts_to_sub2api(body.selectors)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"promotion": result}

    @router.post("/api/partial-accounts/import-cpa")
    def import_partial_accounts_cpa(body: PartialAccountSelectionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        accounts = partial_account_store.select_partial_accounts(body.selectors, include_secret=True)
        try:
            result = cpa_store.import_account_dicts_to_cpa(accounts)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        imported_at = str(result.get("imported_at") or "")
        for item in result.get("results") or []:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip()
            if not email:
                continue
            partial_account_store.update_partial_account(email, {
                "cpa_imported_at": imported_at,
                "cpa_import_status": "ok" if item.get("ok") else "failed",
                "cpa_auth_file": str(item.get("filename") or ""),
                "cpa_last_error": "" if item.get("ok") else str(item.get("response") or item.get("error") or "unknown")[:800],
            })
        return {"cpa": result, "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True)}

    @router.post("/api/partial-accounts/clear")
    async def clear_partial_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_account_pool": partial_account_store.clear_partial_accounts()}

    @router.post("/api/partial-accounts/health/check")
    def check_partial_accounts_health(body: PartialAccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "partial_health": partial_account_health_service.check_partial_accounts(
                body.selectors,
                refresh_quota=bool(body.refresh_quota),
                concurrency=int(body.concurrency or 8),
                mode=body.mode or "",
                method=body.method or "auto",
            )
        }

    @router.post("/api/partial-accounts/quota/check")
    def check_partial_accounts_quota(body: PartialAccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "partial_health": partial_account_health_service.check_partial_accounts(
                body.selectors,
                refresh_quota=True,
                concurrency=int(body.concurrency or 8),
                mode=body.mode or "",
                method="local",
            )
        }

    @router.get("/api/partial-accounts/health")
    async def get_partial_accounts_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_health": partial_account_health_service.partial_account_health_scheduler.get_state()}

    @router.post("/api/partial-accounts/health/settings")
    async def update_partial_accounts_health_settings(
        body: PartialAccountHealthSettingsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {
            "partial_health": partial_account_health_service.partial_account_health_scheduler.update_settings(
                body.model_dump(exclude_none=True)
            )
        }

    @router.post("/api/partial-accounts/health/start-schedule")
    async def start_partial_accounts_health_schedule(
        body: PartialAccountHealthSettingsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {
            "partial_health": partial_account_health_service.partial_account_health_scheduler.start_schedule(
                body.model_dump(exclude_none=True)
            )
        }

    @router.post("/api/partial-accounts/health/stop-schedule")
    async def stop_partial_accounts_health_schedule(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_health": partial_account_health_service.partial_account_health_scheduler.stop_schedule()}

    @router.post("/api/partial-accounts/health/start")
    async def start_partial_accounts_health(body: PartialAccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "partial_health": partial_account_health_service.partial_account_health_scheduler.start_task(
                body.model_dump(exclude_none=True)
            )
        }

    @router.post("/api/partial-accounts/health/stop")
    async def stop_partial_accounts_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_health": partial_account_health_service.partial_account_health_scheduler.stop_task()}

    @router.patch("/api/partial-accounts/{selector}")
    async def update_partial_account(selector: str, body: PartialAccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        current_items = partial_account_store.list_partial_accounts(include_secret=True)
        target = None
        selector_value = unquote(selector).strip().lower()
        for item in current_items:
            if selector_value in {str(item.get("id") or "").lower(), str(item.get("email") or "").strip().lower()}:
                target = item
                break
        if target is None:
            raise HTTPException(status_code=404, detail="partial account not found")
        updates = body.model_dump(exclude_none=True)
        updated = partial_account_store.update_partial_account(selector_value, updates)
        if updated is None:
            raise HTTPException(status_code=400, detail="partial account update would make the record invalid")
        return {"partial_account": updated, "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True)}

    @router.delete("/api/partial-accounts/{selector}")
    async def delete_partial_account(selector: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"partial_account_pool": partial_account_store.delete_partial_account(unquote(selector))}

    @router.post("/api/partial-accounts/{selector}/refresh-token")
    def refresh_partial_account_token(selector: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.refresh_partial_account_token(unquote(selector))
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"partial_refresh": result}

    @router.post("/api/account-oauth/start")
    async def start_account_oauth(body: AccountOAuthStartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.start_account_oauth(
                source=body.source,
                selector=body.selector,
                redirect_uri=body.redirect_uri or "",
            )
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"oauth": result}

    @router.post("/api/account-oauth/complete")
    def complete_account_oauth(body: AccountOAuthCompleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.complete_account_oauth(job_id=body.job_id, callback=body.callback)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"oauth": result}

    @router.post("/api/account-oauth/automatic")
    def acquire_account_oauth_automatically(body: AccountOAuthStartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.acquire_account_oauth_automatically(
                source=body.source,
                selector=body.selector,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"oauth": result}

    @router.post("/api/partial-accounts/{selector}/import-sub2api")
    def import_partial_account_sub2api(selector: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = account_oauth_service.import_partial_account_to_sub2api(unquote(selector))
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"promotion": result}

    @router.get("/api/accounts/inventory")
    async def get_account_inventory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.post("/api/accounts/import")
    def import_accounts(body: AccountImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = import_registered_accounts(body.text)
        settings = account_inventory_service.account_inventory_service.get(include_pool=False).get("settings") or {}
        auto_health = bool(body.auto_health_check) or bool(settings.get("import_auto_health_check"))
        drop_banned = bool(body.drop_banned) or bool(settings.get("import_drop_banned"))
        changed_selectors = pool.get("changed_account_ids") or pool.get("changed_emails") or []
        if auto_health and changed_selectors:
            inventory = account_inventory_service.account_inventory_service.start({
                "emails": changed_selectors,
                "method": body.health_method or settings.get("method") or "auto",
                "drop_banned": drop_banned,
                "refresh_quota": bool(settings.get("refresh_quota_on_health", True)),
                "model_id": str(settings.get("model_id") or ""),
                "prompt": str(settings.get("prompt") or "hi"),
                "mode": str(settings.get("mode") or ""),
                "concurrency": int(settings.get("concurrency") or 10),
                "task_label": "导入后自动测活",
            }, include_pool=False)
            return {"account_pool": pool, "inventory": inventory, "auto_health_started": True}
        return {"account_pool": pool, "inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.get("/api/accounts/health")
    async def get_accounts_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.post("/api/accounts/health/settings")
    async def update_accounts_health_settings(body: AccountInventorySettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.update_settings(body.model_dump(exclude_none=True), include_pool=False)}

    @router.post("/api/accounts/health/check")
    def check_accounts_health(body: AccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = account_inventory_service.account_inventory_service.check_accounts(
            emails=body.emails or [],
            method=body.method or "auto",
            drop_banned=bool(body.drop_banned),
            refresh_quota=body.refresh_quota is not False,
            model_id=body.model_id or "",
            prompt=body.prompt or "hi",
            mode=body.mode or "",
            task_label="测活+额度" if body.refresh_quota is not False else "账号测活",
            concurrency=body.concurrency,
        )
        return {"inventory_health": result, "account_pool": result.get("account_pool") or account_pool(), "inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.post("/api/accounts/health/start")
    async def start_accounts_health(body: AccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.start(body.model_dump(exclude_none=True), include_pool=False)}

    @router.post("/api/accounts/health/stop")
    async def stop_accounts_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.stop(include_pool=False)}

    @router.post("/api/accounts/health/start-schedule")
    async def start_accounts_health_schedule(body: AccountInventorySettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.start_schedule(body.model_dump(exclude_none=True), include_pool=False)}

    @router.post("/api/accounts/health/stop-schedule")
    async def stop_accounts_health_schedule(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"inventory": account_inventory_service.account_inventory_service.stop_schedule(include_pool=False)}

    @router.post("/api/accounts/quota/check")
    def check_accounts_quota(body: AccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = account_inventory_service.account_inventory_service.check_accounts(
            emails=body.emails or [],
            method=body.method or "local",
            drop_banned=bool(body.drop_banned),
            refresh_quota=True,
            model_id=body.model_id or "",
            prompt=body.prompt or "hi",
            mode=body.mode or "",
            task_label="同步额度",
            concurrency=body.concurrency,
        )
        return {"inventory_health": result, "account_pool": result.get("account_pool") or account_pool(), "inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.post("/api/accounts/type/refresh")
    def refresh_accounts_type(body: AccountHealthRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = account_inventory_service.account_inventory_service.check_accounts(
            emails=body.emails or [],
            method="local",
            drop_banned=bool(body.drop_banned),
            refresh_quota=False,
            model_id=body.model_id or "",
            prompt=body.prompt or "hi",
            mode=body.mode or "refresh_type",
            task_label="刷新类型",
            concurrency=body.concurrency,
        )
        return {"inventory_health": result, "account_pool": result.get("account_pool") or account_pool(), "inventory": account_inventory_service.account_inventory_service.get(include_pool=False)}

    @router.post("/api/accounts/prune-banned")
    async def prune_accounts_banned(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return account_inventory_service.account_inventory_service.prune_banned()

    @router.post("/api/accounts/clear")
    async def clear_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"account_pool": clear_registered_accounts()}

    @router.delete("/api/accounts/{email}")
    async def delete_account(email: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        selector = unquote(email)
        account_pool_result = delete_registered_account(selector)
        account_inventory_service.account_inventory_service.forget_account(selector)
        return {"account_pool": account_pool_result}

    @router.patch("/api/accounts/{email}")
    async def update_account(email: str, body: dict, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = update_registered_account(unquote(email), body)
        if result is None:
            raise HTTPException(status_code=404, detail="account not found")
        return {"account": result, "account_pool": account_pool()}

    @router.post("/api/accounts/{email}/refresh-token")
    async def refresh_account_token(email: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        selector = unquote(email)
        accounts = list_registered_accounts(include_secret=True)
        account = None
        for a in accounts:
            if account_matches_selector(a, selector):
                account = a
                break
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        refresh_token = str(account.get("refresh_token") or "").strip()
        if not refresh_token:
            update_registered_account(selector, {"oauth_status": "missing_refresh_token", "oauth_refresh_status": "refresh_token_missing", "oauth_refresh_error": "账号没有 refresh_token", "token_refresh_required": True})
            raise HTTPException(status_code=400, detail="account has no refresh_token")
        proxy = str(account.get("proxy") or "").strip()
        try:
            from services.account_inventory_service import _classify_failure, _compact_error, _local_health_and_quota, _refresh_platform_tokens, _token_context
            refreshed = _refresh_platform_tokens(refresh_token, client_id=str(account.get("client_id") or ""), proxy=proxy)
        except Exception as e:
            from services.account_inventory_service import _classify_failure, _compact_error
            error_text = _compact_error(e, 800)
            rt_status = _classify_failure(0, f"refresh_token {error_text}", default="refresh_token_refresh_failed")
            update_registered_account(selector, {"oauth_status": "refresh_failed", "oauth_refresh_status": rt_status, "oauth_refresh_error": error_text, "token_refresh_required": True})
            raise HTTPException(status_code=400, detail=f"{rt_status}: {error_text}") from e
        new_access = str(refreshed.get("access_token") or "").strip()
        new_refresh = str(refreshed.get("refresh_token") or refresh_token).strip()
        new_id = str(refreshed.get("id_token") or "").strip()
        updates = {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "id_token": new_id,
            "client_id": str(refreshed.get("client_id") or account.get("client_id") or ""),
            "oauth_refreshed_at": refreshed.get("refreshed_at"),
            "oauth_status": "refreshed_manual",
            "oauth_refresh_status": "ok",
            "oauth_refresh_error": "",
            "token_refresh_required": False,
        }
        if new_access:
            try:
                ctx = _token_context(new_access, new_id)
                if ctx.get("plan_type"):
                    updates["plan_type"] = ctx["plan_type"]
                    updates["account_type"] = ctx["plan_type"]
                if ctx.get("chatgpt_account_id"):
                    updates["chatgpt_account_id"] = ctx["chatgpt_account_id"]
                if ctx.get("chatgpt_user_id"):
                    updates["chatgpt_user_id"] = ctx["chatgpt_user_id"]
            except Exception:
                pass
        result = update_registered_account(selector, updates)
        try:
            probe_account = {**account, **updates}
            probe_result = _local_health_and_quota(probe_account, refresh_quota=True, proxy=proxy, force_refresh_token=False)
            probe_updates = probe_result.get("updates") if isinstance(probe_result, dict) else {}
            if isinstance(probe_updates, dict) and probe_updates:
                result = update_registered_account(selector, probe_updates) or result
        except Exception as e:
            logger.warning("manual token refresh succeeded but plan probe failed for %s: %s", selector, e)
        return {"account": result, "account_pool": account_pool()}

    @router.post("/api/accounts/export-sub2api")
    async def export_accounts_sub2api(body: AccountSub2ApiImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"sub2api": sub2api_store.export_sub2api_data(body.emails)}

    @router.post("/api/accounts/import-sub2api")
    def import_accounts_sub2api(body: AccountSub2ApiImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = sub2api_store.import_accounts_to_sub2api(body.emails)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"sub2api": result, "account_pool": account_pool()}

    @router.post("/api/accounts/import-cpa")
    def import_accounts_cpa(body: AccountCpaImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = cpa_store.import_accounts_to_cpa(body.emails)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"cpa": result, "account_pool": account_pool()}

    @router.get("/api/sub2api-config")
    async def get_sub2api_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"sub2api_config": sub2api_store.get_sub2api_config()}

    @router.post("/api/sub2api-config")
    async def update_sub2api_config(body: Sub2ApiConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = body.model_dump(exclude_none=True)
        config = sub2api_store.update_sub2api_config(updates)
        return {"sub2api_config": config, "register": register_service.get()}

    @router.post("/api/sub2api/connection/test")
    async def test_sub2api_connection(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"sub2api_connection": sub2api_store.test_sub2api_connection()}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/sub2api/groups")
    async def get_sub2api_groups(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"sub2api_groups": sub2api_store.list_sub2api_groups()}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/sub2api/accounts")
    async def get_sub2api_accounts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1),
        group_id: str = "",
        search: str = "",
        status: str = "",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return {"sub2api_accounts": sub2api_store.list_sub2api_accounts(page=page, page_size=page_size, group_id=group_id, search=search, status=status)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/sub2api/accounts/test")
    async def test_sub2api_accounts(body: Sub2ApiAccountsTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"sub2api_test": sub2api_store.test_sub2api_accounts(
                body.account_ids,
                group_id=body.group_id,
                model_id=body.model_id or "",
                prompt=body.prompt or "hi",
                mode=body.mode or "",
                concurrency=body.concurrency,
                apply_policy=bool(body.apply_policy),
            )}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/sub2api/accounts/usage")
    async def get_sub2api_accounts_usage(body: WarehouseAccountIdsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"sub2api_usage": sub2api_store.get_sub2api_accounts_usage(body.account_ids or [])}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/sub2api/accounts/action")
    async def act_on_sub2api_accounts(body: WarehouseAccountActionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"sub2api_action": sub2api_store.act_on_sub2api_accounts(body.account_ids or [], body.action)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/cpa-config")
    async def get_cpa_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"cpa_config": cpa_store.get_cpa_config()}

    @router.post("/api/cpa-config")
    async def update_cpa_config(body: CpaConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"cpa_config": cpa_store.update_cpa_config(body.model_dump(exclude_none=True)), "register": register_service.get()}

    @router.post("/api/cpa/connection/test")
    async def test_cpa_connection(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"cpa_connection": cpa_store.test_cpa_connection()}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/cpa/accounts")
    async def get_cpa_accounts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1),
        search: str = "",
        status: str = "all",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return {"cpa_accounts": cpa_store.list_cpa_accounts(page=page, page_size=page_size, search=search, status=status)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/cpa/accounts/test")
    async def test_cpa_accounts(body: CpaAccountsTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"cpa_test": cpa_store.test_cpa_accounts(body.names, apply_policy=bool(body.apply_policy))}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/cpa/accounts/action")
    async def act_on_cpa_accounts(body: WarehouseAccountActionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"cpa_action": cpa_store.act_on_cpa_accounts(body.names or [], body.action)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/k12")
    async def get_k12_inventory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"k12": k12_inventory_service.get()}

    @router.post("/api/k12/config")
    async def update_k12_config(body: K12ConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = body.model_dump(exclude_none=True)
        config = k12_config_store.update_k12_config(updates)
        register = register_service.get()
        if "enabled" in updates:
            enabled = bool(config.get("enabled"))
            register_mode = str(register.get("register_mode") or "free")
            register_updates: dict[str, Any] = {"k12_mode": enabled}
            if enabled:
                register_updates["register_mode"] = "k12"
            elif register_mode == "k12":
                register_updates["register_mode"] = "free"
            register = register_service.update(register_updates)
        return {"k12": k12_inventory_service.get(), "register": register}

    @router.post("/api/k12/start")
    async def start_k12_inventory(body: K12StartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"k12": k12_inventory_service.start(body.model_dump(exclude_none=True))}

    @router.post("/api/k12/stop")
    async def stop_k12_inventory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"k12": k12_inventory_service.stop()}

    @router.post("/api/k12/reset")
    async def reset_k12_inventory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"k12": k12_inventory_service.reset()}

    @router.get("/api/plus-link")
    async def get_plus_link_state(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.get()}

    @router.post("/api/plus-link/config")
    async def update_plus_link_config(body: PlusLinkConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.update_config(body.model_dump(exclude_none=True))}

    @router.post("/api/plus-link/start")
    async def start_plus_link(body: PlusLinkStartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.start(body.model_dump(exclude_none=True))}

    @router.post("/api/plus-link/stop")
    async def stop_plus_link(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.stop()}

    @router.post("/api/plus-link/tasks/{task_id}/pause")
    async def pause_plus_link_task(task_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.pause_task(task_id)}

    @router.post("/api/plus-link/tasks/{task_id}/resume")
    async def resume_plus_link_task(task_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.resume_task(task_id)}

    @router.post("/api/plus-link/tasks/{task_id}/cancel")
    async def cancel_plus_link_task(task_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.cancel_task(task_id)}

    @router.post("/api/plus-link/tasks/{task_id}/retry")
    async def retry_plus_link_task(task_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.retry_task(task_id)}

    @router.post("/api/plus-link/clear")
    async def clear_plus_link(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.clear_results()}

    @router.post("/api/plus-link/clear-failed")
    async def clear_failed_plus_link(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.clear_failed_results()}

    @router.post("/api/plus-link/clear-finished")
    async def clear_finished_plus_link(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"plus_link": plus_link_store.plus_link_service.clear_finished_results()}

    @router.get("/api/garbage-health")
    async def get_garbage_health_state(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.get()}

    @router.post("/api/garbage-health/config")
    def update_garbage_health_config(body: GarbageHealthConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.update_config(body.model_dump(exclude_none=True))}

    @router.post("/api/garbage-health/scan")
    def scan_garbage_health(body: GarbageHealthConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.scan(body.model_dump(exclude_none=True))}

    @router.post("/api/garbage-health/select-directory")
    def select_garbage_health_directory(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        state = garbage_health_service.garbage_health_service.get()
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        try:
            return {"directory": garbage_health_service.select_local_directory(str(config.get("input_dir") or ""))}
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @router.post("/api/garbage-health/start")
    def start_garbage_health(body: GarbageHealthStartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.start(body.model_dump(exclude_none=True))}

    @router.post("/api/garbage-health/stop")
    async def stop_garbage_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.stop()}

    @router.post("/api/garbage-health/clear")
    async def clear_garbage_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"garbage_health": garbage_health_service.garbage_health_service.clear()}

    @router.get("/api/garbage-health/export")
    async def export_garbage_health(format: str = "sub2api", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"garbage_export": garbage_health_service.garbage_health_service.export_data(format)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/garbage-health/import-sub2api")
    async def import_garbage_health_sub2api(body: GarbageHealthImportRequest | None = None, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"garbage_import": garbage_health_service.garbage_health_service.import_to_sub2api(body.group_id if body else None)}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/garbage-health/import-cpa")
    async def import_garbage_health_cpa(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"garbage_import": garbage_health_service.garbage_health_service.import_to_cpa_inventory()}
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/proxy-pool")
    async def get_proxy_pool(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"proxy_pool": proxy_pool_store.get_proxy_pool_config()}

    @router.post("/api/proxy-pool")
    async def update_proxy_pool(body: ProxyPoolUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.update_proxy_pool_config(body.model_dump(exclude_none=True)))

    @router.post("/api/proxy-pool/import-subscriptions")
    async def import_proxy_subscriptions(body: ProxySubscriptionImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        items = body.items or []
        if body.url:
            items.append({"name": body.name or body.url, "url": body.url, "allow_private": bool(body.allow_private)})
        elif body.allow_private:
            items = [{**item, "allow_private": bool(item.get("allow_private", True))} for item in items]
        if body.text and not items:
            return proxy_pool_changed(proxy_pool_store.import_subscription_text(body.text, name=body.name or "手动订阅"))
        return proxy_pool_changed(proxy_pool_store.add_subscriptions(items, fetch=body.fetch is not False))

    @router.post("/api/proxy-pool/import-nodes")
    async def import_proxy_nodes(body: ProxyNodesImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.import_node_text(body.text))

    @router.post("/api/proxy-pool/sync-mihomo")
    async def sync_proxy_pool_mihomo(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.sync_mihomo_nodes())

    @router.get("/api/proxy-pool/managed-mihomo/status")
    async def managed_mihomo_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"managed_mihomo": managed_mihomo_service.managed_mihomo_manager.status()}

    @router.post("/api/proxy-pool/managed-mihomo/install")
    async def install_managed_mihomo(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        store = proxy_pool_store.get_proxy_pool_config(include_secret=True)
        managed = dict(store.get("managed_mihomo") or {})
        result = managed_mihomo_service.managed_mihomo_manager.install(str(managed.get("executable") or ""))
        managed["executable"] = str(result.get("executable") or "")
        proxy_pool = proxy_pool_store.update_proxy_pool_config({"managed_mihomo": managed})
        return {"managed_mihomo": managed_mihomo_service.managed_mihomo_manager.status(), "proxy_pool": proxy_pool}

    @router.post("/api/proxy-pool/managed-mihomo/start")
    async def start_managed_mihomo(body: ManagedMihomoRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        reject_disruptive_proxy_action()
        store = proxy_pool_store.get_proxy_pool_config(include_secret=True)
        requested_threads = body.threads if body.threads is not None else register_service.get().get("threads")
        threads = max(1, min(50, int(requested_threads or 1)))
        status = managed_mihomo_service.managed_mihomo_manager.start(threads, store)
        return {"managed_mihomo": status, "proxy_pool": proxy_pool_store.get_proxy_pool_config()}

    @router.post("/api/proxy-pool/managed-mihomo/stop")
    async def stop_managed_mihomo(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        reject_disruptive_proxy_action()
        return {"managed_mihomo": managed_mihomo_service.managed_mihomo_manager.stop(), "proxy_pool": proxy_pool_store.get_proxy_pool_config()}

    @router.post("/api/proxy-pool/preview-assignments")
    async def preview_proxy_assignments(body: ProxyAssignmentPreviewRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        requested_threads = body.threads if body.threads is not None else register_service.get().get("threads")
        threads = max(1, min(50, int(requested_threads or 1)))
        tasks = max(1, min(100, int(body.tasks or threads)))
        assignments = [
            proxy_pool_store.assign_proxy(index, total_threads=threads, slot_index=(index - 1) % threads, purpose="preview")
            for index in range(1, tasks + 1)
        ]
        return {
            "assignments": assignments,
            "managed_mihomo": managed_mihomo_service.managed_mihomo_manager.status(),
            "proxy_pool": proxy_pool_store.get_proxy_pool_config(),
        }

    @router.post("/api/proxy-pool/test")
    async def test_proxy_node(body: ProxyNodeTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"proxy_test": proxy_pool_store.test_node(body.node_id or "", body.proxy_url or ""), "proxy_pool": proxy_pool_store.get_proxy_pool_config()}

    @router.post("/api/proxy-pool/clear-nodes")
    async def clear_proxy_nodes(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.clear_nodes())

    @router.delete("/api/proxy-pool/nodes/{node_id}")
    async def delete_proxy_node(node_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.delete_node(node_id))

    @router.delete("/api/proxy-pool/subscriptions/{subscription_id}")
    async def delete_proxy_subscription(subscription_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return proxy_pool_changed(proxy_pool_store.delete_subscription(subscription_id))

    @router.get("/api/proxy-controller")
    async def get_proxy_controller(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"proxy_controller": proxy_controller.get_proxy_controller_config()}

    @router.post("/api/proxy-controller")
    async def update_proxy_controller(body: ProxyControllerRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"proxy_controller": proxy_controller.update_proxy_controller_config(body.model_dump(exclude_none=True))}

    @router.post("/api/proxy-controller/rotate")
    async def rotate_proxy_controller(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        reject_disruptive_proxy_action()
        return {"proxy_controller": proxy_controller.rotate_proxy("manual")}

    return router
