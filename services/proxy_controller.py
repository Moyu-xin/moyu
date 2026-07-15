from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR

PROXY_CONTROLLER_FILE = DATA_DIR / "proxy_controller.json"

DEFAULT_PROXY_CONTROLLER = {
    "enabled": True,
    "api_base": "http://127.0.0.1:9097",
    "secret": "set-your-secret",
    "selector": "GLOBAL",
    "skip_names": ["DIRECT", "REJECT", "GLOBAL"],
    "last_selected": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    data = read_json(PROXY_CONTROLLER_FILE, {}, expected_schema_version=1)
    if isinstance(data, dict):
        return {**DEFAULT_PROXY_CONTROLLER, **data}
    return dict(DEFAULT_PROXY_CONTROLLER)


def _save(cfg: dict[str, Any]) -> None:
    PROXY_CONTROLLER_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(PROXY_CONTROLLER_FILE, {**DEFAULT_PROXY_CONTROLLER, **cfg, "updated_at": _now()}, schema_version=1)


def get_proxy_controller_config() -> dict[str, Any]:
    cfg = _load()
    redacted = dict(cfg)
    if redacted.get("secret"):
        redacted["secret"] = "***"
    return redacted


def update_proxy_controller_config(updates: dict[str, Any]) -> dict[str, Any]:
    cfg = _load()
    for key in ("enabled", "api_base", "secret", "selector", "skip_names"):
        if key in updates:
            cfg[key] = updates[key]
    if isinstance(cfg.get("skip_names"), str):
        cfg["skip_names"] = [item.strip() for item in str(cfg["skip_names"]).split(",") if item.strip()]
    _save(cfg)
    return get_proxy_controller_config()


def _request(cfg: dict[str, Any], method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    api_base = str(cfg.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("proxy controller api_base is empty")
    url = f"{api_base}{path}"
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    secret = str(cfg.get("secret") or "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"proxy controller HTTP {error.code}: {detail}") from error
    except Exception as error:
        raise RuntimeError(f"proxy controller request failed: {error}") from error
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {"raw": raw}
    return data if isinstance(data, dict) else {"data": data}


def _proxy_items(data: dict[str, Any]) -> dict[str, Any]:
    proxies = data.get("proxies")
    return proxies if isinstance(proxies, dict) else {}


def _select_group(cfg: dict[str, Any], proxies: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    wanted = str(cfg.get("selector") or "GLOBAL").strip() or "GLOBAL"
    item = proxies.get(wanted)
    if isinstance(item, dict) and isinstance(item.get("all"), list):
        return wanted, item
    for name, value in proxies.items():
        if isinstance(value, dict) and isinstance(value.get("all"), list) and value.get("all"):
            ptype = str(value.get("type") or "").lower()
            if ptype in {"selector", "urltest", "fallback", "loadbalance", "relay"} or name.upper() in {"GLOBAL", "PROXY", "节点选择"}:
                return str(name), value
    raise RuntimeError(f"未找到可切换的代理组：{wanted}")


def rotate_proxy(reason: str = "") -> dict[str, Any]:
    cfg = _load()
    if not bool(cfg.get("enabled")):
        return {"ok": False, "skipped": True, "message": "proxy controller disabled"}
    data = _request(cfg, "GET", "/proxies")
    proxies = _proxy_items(data)
    group_name, group = _select_group(cfg, proxies)
    raw_candidates = [str(item) for item in (group.get("all") or []) if str(item).strip()]
    skip_names = {str(item).strip().upper() for item in (cfg.get("skip_names") or [])}
    candidates = [item for item in raw_candidates if item.strip().upper() not in skip_names and item in proxies]
    if not candidates:
        candidates = [item for item in raw_candidates if item.strip().upper() not in skip_names]
    if not candidates:
        raise RuntimeError(f"代理组 {group_name} 没有可切换节点")
    current = str(group.get("now") or cfg.get("last_selected") or "").strip()
    if current in candidates:
        next_name = candidates[(candidates.index(current) + 1) % len(candidates)]
    else:
        next_name = candidates[0]
    encoded_group = urllib.parse.quote(group_name, safe="")
    _request(cfg, "PUT", f"/proxies/{encoded_group}", {"name": next_name})
    cfg["selector"] = group_name
    cfg["last_selected"] = next_name
    cfg["last_reason"] = str(reason or "")
    _save(cfg)
    return {"ok": True, "group": group_name, "selected": next_name, "previous": current, "candidates": len(candidates)}
