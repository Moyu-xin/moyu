from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR

K12_CONFIG_FILE = DATA_DIR / "k12_config.json"
LEGACY_SUB2API_CONFIG_FILE = DATA_DIR / "sub2api_config.json"

DEFAULT_OUTLOOK_TEAM_IDS = ["7bf3a2ce-3298-40b0-ac9b-3c922a5a91a6"]
DEFAULT_GMAIL_TEAM_IDS = [
    "59208eb6-ec43-4d87-9289-dbd9e250bdd6",
    "47336c9d-7607-4478-b37c-018049af1e46",
    "2c82c020-e1bc-4363-9502-a6794405f793",
    "9901799e-e832-48b1-9278-9abe73168708",
    "c72dcdb4-63a0-40b7-b0bb-ccce3ca54984",
    "2b636e76-a87b-4222-b536-2dc4a545109f",
    "4779b1d7-3109-4ecb-957f-80262f4d7161",
    "ae67aa09-f3d3-4895-977d-9ca44ed1d996",
    "6daa08c1-59c8-4e06-9bc8-9d7246a63057",
    "521ffc8f-9612-4950-84ed-95773138eca6",
    "ff598c4d-ccaf-40c1-bfaa-cb94565764b1",
    "cf8e512d-1f3b-4603-950c-3d9758a8b435",
]

LEGACY_KEYS = {
    "k12_mode",
    "k12_team_ids",
    "k12_join_route",
    "k12_accept_invite",
    "k12_invite_timeout",
}

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": False,
        "team_ids": {
            "outlook": list(DEFAULT_OUTLOOK_TEAM_IDS),
            "gmail": list(DEFAULT_GMAIL_TEAM_IDS),
        },
        "join_route": "request",
        "accept_invite": True,
        "invite_timeout": 180,
        "migrated_from_sub2api_at": "",
        "updated_at": "",
    }


def _read_dict(path: Path) -> dict[str, Any]:
    data = read_json(path, {}, expected_schema_version=1)
    return data if isinstance(data, dict) else {}


def _as_team_ids(value: Any) -> dict[str, list[str]]:
    defaults = _default_config()["team_ids"]
    raw = value if isinstance(value, dict) else {}
    result: dict[str, list[str]] = {}
    for kind in ("outlook", "gmail"):
        current = raw.get(kind, defaults[kind])
        if isinstance(current, str):
            values = current.replace("\r", "\n").replace(",", "\n").split("\n")
        elif isinstance(current, list):
            values = current
        else:
            values = []
        deduped: list[str] = []
        for item in values:
            team_id = str(item or "").strip()
            if team_id and team_id not in deduped:
                deduped.append(team_id)
        result[kind] = deduped
    return result


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = _default_config()
    cfg.update({key: value for key, value in raw.items() if key in cfg})
    cfg["schema_version"] = 1
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["team_ids"] = _as_team_ids(cfg.get("team_ids"))
    cfg["join_route"] = str(cfg.get("join_route") or "request").strip().strip("/") or "request"
    cfg["accept_invite"] = bool(cfg.get("accept_invite"))
    try:
        cfg["invite_timeout"] = max(10, min(3600, int(cfg.get("invite_timeout") or 180)))
    except Exception:
        cfg["invite_timeout"] = 180
    cfg["migrated_from_sub2api_at"] = str(cfg.get("migrated_from_sub2api_at") or "")
    cfg["updated_at"] = str(cfg.get("updated_at") or "")
    return cfg


def _legacy_config(raw: dict[str, Any]) -> dict[str, Any]:
    field_map = {
        "k12_mode": "enabled",
        "k12_team_ids": "team_ids",
        "k12_join_route": "join_route",
        "k12_accept_invite": "accept_invite",
        "k12_invite_timeout": "invite_timeout",
    }
    return {target: raw[source] for source, target in field_map.items() if source in raw}


def _remove_legacy_fields(path: Path) -> None:
    raw = _read_dict(path)
    if not raw or not any(key in raw for key in LEGACY_KEYS):
        return
    cleaned = {key: value for key, value in raw.items() if key not in LEGACY_KEYS}
    write_json(path, cleaned, schema_version=1)


def ensure_migrated() -> dict[str, Any]:
    with _lock:
        current = _read_dict(K12_CONFIG_FILE)
        if current:
            cfg = _normalize(current)
            _remove_legacy_fields(LEGACY_SUB2API_CONFIG_FILE)
            return cfg

        legacy = _read_dict(LEGACY_SUB2API_CONFIG_FILE)
        has_legacy = any(key in legacy for key in LEGACY_KEYS)
        cfg = _normalize(_legacy_config(legacy) if has_legacy else {})
        now = _now()
        cfg["updated_at"] = now
        if has_legacy:
            cfg["migrated_from_sub2api_at"] = now
        write_json(K12_CONFIG_FILE, cfg, schema_version=1)
        _remove_legacy_fields(LEGACY_SUB2API_CONFIG_FILE)
        return cfg


def get_k12_config() -> dict[str, Any]:
    return json.loads(json.dumps(ensure_migrated(), ensure_ascii=False))


def update_k12_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cfg = ensure_migrated()
        for key in ("enabled", "team_ids", "join_route", "accept_invite", "invite_timeout"):
            if key in updates:
                cfg[key] = updates[key]
        cfg["updated_at"] = _now()
        cfg = _normalize(cfg)
        write_json(K12_CONFIG_FILE, cfg, schema_version=1)
        return json.loads(json.dumps(cfg, ensure_ascii=False))


def choose_k12_team_id(email: str, cfg: dict[str, Any] | None = None) -> str:
    current = _normalize(cfg or ensure_migrated())
    domain = str(email or "").strip().lower().rsplit("@", 1)[-1]
    if domain == "gmail.com":
        kind = "gmail"
    elif domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        kind = "outlook"
    else:
        return ""
    values = current.get("team_ids") if isinstance(current.get("team_ids"), dict) else {}
    pool = values.get(kind) if isinstance(values.get(kind), list) else []
    return str(pool[0]).strip() if pool else ""
