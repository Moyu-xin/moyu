from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

from services import proxy_pool_store
from services.proxy_service import proxy_settings


def is_local_target(url: str) -> bool:
    value = str(url or "").strip()
    if not value:
        return False
    parsed = urlparse(value if "://" in value else f"http://{value}")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "host.docker.internal"} or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def assign_task_proxy(
    task_index: int,
    *,
    purpose: str,
    target_url: str = "",
    account: dict[str, Any] | None = None,
    fallback: str = "",
    total_threads: int = 1,
    slot_index: int = 0,
) -> dict[str, Any]:
    if is_local_target(target_url):
        return {
            "proxy": "",
            "source": "local_direct",
            "label": "本机/内网直连",
            "slot": max(1, int(slot_index or 0) + 1),
            "purpose": str(purpose or ""),
        }
    return proxy_pool_store.assign_proxy(
        task_index,
        fallback=fallback,
        total_threads=max(1, int(total_threads or 1)),
        slot_index=max(0, int(slot_index or 0)),
        purpose=purpose,
        account=account,
        validate_node=False,
    )


def build_task_session_kwargs(
    task_index: int,
    *,
    purpose: str,
    target_url: str = "",
    account: dict[str, Any] | None = None,
    fallback: str = "",
    total_threads: int = 1,
    slot_index: int = 0,
    impersonate: str = "chrome",
    verify: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assignment = assign_task_proxy(
        task_index,
        purpose=purpose,
        target_url=target_url,
        account=account,
        fallback=fallback,
        total_threads=total_threads,
        slot_index=slot_index,
    )
    if str(assignment.get("source") or "") == "local_direct":
        return {"impersonate": impersonate, "verify": verify}, assignment
    kwargs = proxy_settings.build_session_kwargs(
        account=account,
        proxy=str(assignment.get("proxy") or ""),
        upstream=True,
        impersonate=impersonate,
        verify=verify,
    )
    return kwargs, assignment
