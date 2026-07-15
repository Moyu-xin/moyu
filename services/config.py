from __future__ import annotations

import copy
import hmac
import json
import os
import threading
from pathlib import Path

from services.atomic_json_store import read_json, write_json

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("CHATGPT2API_DATA_DIR") or (BASE_DIR / "data")).expanduser().resolve()
CONFIG_FILE = Path(os.getenv("CHATGPT2API_CONFIG_FILE") or (BASE_DIR / "config.json")).expanduser().resolve()
VERSION_FILE = BASE_DIR / "VERSION"

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists() or path.is_dir():
        return {}
    data = read_json(path, {}, expected_schema_version=1)
    return data if isinstance(data, dict) else {}


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _normalize_status_codes(value: object) -> list[int]:
    source = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    codes: list[int] = []
    for item in source:
        if isinstance(item, bool):
            continue
        try:
            code = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= code <= 599 and code not in codes:
            codes.append(code)
    return codes or list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    existing_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_clearance

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "skip_ssl_verify": _normalize_bool(source.get("skip_ssl_verify"), bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"])),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip(),
            "browser": str(clearance_source.get("browser") or default_clearance["browser"]).strip(),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(clearance_source.get("timeout_sec"), int(default_clearance["timeout_sec"]), 1),
            "refresh_interval": _normalize_positive_int(clearance_source.get("refresh_interval"), int(default_clearance["refresh_interval"]), 60),
            "warm_up_on_start": _normalize_bool(clearance_source.get("warm_up_on_start"), bool(default_clearance["warm_up_on_start"])),
        },
    }


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


class ConfigStore:
    """最小化本地配置存储，保留注册相关字段和 Cloudflare clearance。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        if not self.auth_key:
            raise ValueError("auth-key 未配置，请设置 CHATGPT2API_AUTH_KEY 或在 config.json 中填写 auth-key")

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        write_json(self.path, self.data, schema_version=1)

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def auth_key_source(self) -> str:
        return "environment" if _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY")) else "config"

    def update_auth_key(self, current_key: str, new_key: str) -> None:
        if self.auth_key_source == "environment":
            raise RuntimeError("CHATGPT2API_AUTH_KEY is active; update the environment variable and restart the service")
        normalized_current = _normalize_auth_key(current_key)
        normalized_new = _normalize_auth_key(new_key)
        if not hmac.compare_digest(normalized_current, self.auth_key):
            raise ValueError("Current admin key is invalid")
        with self._lock:
            self.data["auth-key"] = normalized_new
            self._save()

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        runtime = copy.deepcopy(self.get_proxy_runtime_settings())
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
        return runtime

    def update_proxy_runtime(self, settings: dict[str, object]) -> dict[str, object]:
        incoming = dict(settings or {})
        previous_clearance = self.get_proxy_runtime_settings().get("clearance")
        if isinstance(previous_clearance, dict):
            incoming["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
            incoming["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
        self.data["proxy_runtime"] = _normalize_proxy_runtime_settings(incoming)
        self._save()
        return self.get_public_proxy_runtime_settings()


config = ConfigStore(CONFIG_FILE)
