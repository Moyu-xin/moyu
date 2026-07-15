from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from curl_cffi import requests

from services import k12_config_store
from services.proxy_service import proxy_settings
from services.register import mail_provider

CHATGPT_BASE = "https://chatgpt.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _response_detail(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        data = resp.json()
    except Exception:
        data = {"body": str(getattr(resp, "text", "") or "")[:600]}
    return {
        "status_code": int(getattr(resp, "status_code", 0) or 0),
        "url": str(getattr(resp, "url", "") or "")[:300],
        "data": data,
    }


def _headers(access_token: str, device_id: str, user_agent: str = DEFAULT_USER_AGENT) -> dict[str, str]:
    return {
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "zh-CN",
        "origin": CHATGPT_BASE,
        "referer": f"{CHATGPT_BASE}/",
        "user-agent": user_agent or DEFAULT_USER_AGENT,
    }


def request_k12_join(
    *,
    session: requests.Session,
    access_token: str,
    email: str,
    mailbox: dict[str, Any],
    mail_config: dict[str, Any],
    index: int,
    user_agent: str = DEFAULT_USER_AGENT,
    log_step=None,
    force_enabled: bool = False,
) -> dict[str, Any]:
    def emit(text: str, color: str = "") -> None:
        if callable(log_step):
            log_step(index, text, color)

    cfg = k12_config_store.get_k12_config()
    if force_enabled:
        cfg["enabled"] = True
    if not cfg.get("enabled"):
        return {"enabled": False, "status": "disabled"}
    team_id = k12_config_store.choose_k12_team_id(email, cfg)
    if not team_id:
        return {"enabled": True, "status": "skipped_unsupported_domain", "email": email}
    if not str(access_token or "").strip():
        return {"enabled": True, "status": "skipped_missing_access_token", "email": email, "team_id": team_id}

    route = str(cfg.get("join_route") or "request").strip().strip("/") or "request"
    device_id = str(mailbox.get("_k12_device_id") or uuid.uuid4())
    mailbox["_k12_device_id"] = device_id
    mailbox["_code_not_before"] = datetime.now(timezone.utc)
    url = f"{CHATGPT_BASE}/backend-api/accounts/{team_id}/invites/{route}"
    emit(f"K12 模式：请求加入 team {team_id}")
    resp = session.post(url, headers=_headers(access_token, device_id, user_agent), data="", timeout=45, verify=proxy_settings.verify_tls())
    request_detail = _response_detail(resp)
    ok = 200 <= int(getattr(resp, "status_code", 0) or 0) < 300
    result: dict[str, Any] = {
        "enabled": True,
        "team_id": team_id,
        "workspace_id": team_id,
        "route": route,
        "requested_at": _now(),
        "request": request_detail,
        "status": "request_sent" if ok else "request_failed",
    }
    if not ok:
        emit(f"K12 加入请求失败：HTTP {getattr(resp, 'status_code', 'unknown')}", "yellow")
        return result

    if route.lower() == "accept":
        result["status"] = "accepted_attempted"
        emit("K12 accept route completed; skip invite-mail waiting")
        return result

    keyword = "k12-invite"
    timeout = int(cfg.get("invite_timeout") or 180)
    emit(f"K12 模式：等待邀请邮件（{timeout}s）")
    try:
        invite_url = mail_provider.wait_for_link(mail_config, mailbox, keyword=keyword, timeout=timeout)
    except Exception as error:
        result["status"] = "request_sent_mail_error"
        result["mail_error"] = str(error)[:500]
        emit(f"K12 邀请邮件读取失败，已保留 request_sent 状态并继续后续入库：{error}", "yellow")
        return result
    if not invite_url:
        result["status"] = "request_sent_no_invite"
        emit("K12 邀请邮件未在超时内出现，已保留 request_sent 状态", "yellow")
        return result

    result["invite_url"] = invite_url
    result["status"] = "invite_received"
    emit("K12 模式：已读取邀请链接")

    if bool(cfg.get("accept_invite")):
        try:
            accept_resp = session.get(invite_url, headers=_headers(access_token, device_id, user_agent), allow_redirects=True, timeout=60, verify=proxy_settings.verify_tls())
            result["accept"] = _response_detail(accept_resp)
            if 200 <= int(getattr(accept_resp, "status_code", 0) or 0) < 400:
                result["status"] = "accepted_attempted"
                emit("K12 模式：邀请链接已尝试接受")
            else:
                result["status"] = "accept_failed"
                emit(f"K12 接受邀请失败：HTTP {getattr(accept_resp, 'status_code', 'unknown')}", "yellow")
        except Exception as error:
            result["status"] = "accept_failed"
            result["accept_error"] = str(error)
            emit(f"K12 接受邀请异常：{error}", "yellow")
    return result


def summarize_k12_result(result: dict[str, Any]) -> str:
    safe = dict(result or {})
    if safe.get("invite_url"):
        safe["invite_url"] = str(safe["invite_url"])[:120]
    return json.dumps(safe, ensure_ascii=False)[:1000]
