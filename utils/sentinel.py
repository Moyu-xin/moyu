"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
import json
import random
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from curl_cffi.requests import Session


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。"""
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"
    SCRIPT_URL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
    SCRIPT_SRCS = (
        SCRIPT_URL,
        "https://cdn.oaistatic.com/assets/manifest.js",
        "https://cdn.oaistatic.com/assets/vendor.js",
        "https://cdn.oaistatic.com/assets/main.js",
        "https://cdn.oaistatic.com/assets/runtime.js",
        "https://cdn.oaistatic.com/assets/polyfills.js",
    )
    NAVIGATOR_VALUES = {
        "userAgent": "",
        "language": "en-US",
        "languages": "en-US,en",
        "platform": "Win32",
        "vendor": "",
        "vendorSub": "",
        "product": "Gecko",
        "productSub": "20100101",
        "appName": "Netscape",
        "hardwareConcurrency": "8",
        "maxTouchPoints": "0",
        "cookieEnabled": "true",
        "onLine": "true",
        "doNotTrack": "1",
        "pdfViewerEnabled": "true",
    }
    DOCUMENT_KEYS = (
        "location", "referrer", "cookie", "title", "URL", "domain", "body", "head",
        "documentElement", "scripts", "images", "forms", "links", "anchors", "readyState",
        "visibilityState", "hidden", "activeElement", "styleSheets", "fonts", "characterSet",
        "compatMode", "contentType", "implementation", "defaultView", "baseURI", "isConnected",
    )
    WINDOW_KEYS = (
        "location", "navigator", "history", "screen", "document", "window", "self", "top",
        "parent", "frames", "origin", "performance", "crypto", "fetch", "XMLHttpRequest",
        "WebSocket", "localStorage", "sessionStorage", "indexedDB", "console", "postMessage",
        "getComputedStyle", "matchMedia", "visualViewport", "isSecureContext",
    )

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = round(time.time() * 1000 - 1_000_000 + random.uniform(1000, 5000), 1)
        time_origin = round(time.time() * 1000 - 50_000, 1)
        navigator_key = random.choice(tuple(self.NAVIGATOR_VALUES))
        navigator_value = self.NAVIGATOR_VALUES[navigator_key] or self.user_agent
        return [
            3000,
            str(datetime.now(timezone.utc).astimezone()),
            4294705152,
            0,
            self.user_agent,
            random.choice(self.SCRIPT_SRCS),
            None,
            "en-US",
            "en-US,en",
            0,
            f"{navigator_key}\u2212{navigator_value}",
            random.choice(self.DOCUMENT_KEYS),
            random.choice(self.WINDOW_KEYS),
            perf_now,
            self.sid,
            "",
            8,
            time_origin,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        seed = str(random.random())
        return "gAAAAAC" + self._solve(seed, "0", self._get_config())

    def generate_token(self, seed: str, difficulty: str) -> str:
        return "gAAAAAB" + self._solve(str(seed or ""), str(difficulty or "0"), self._get_config())

    def _solve(self, seed: str, difficulty: str, data: list) -> str:
        start = time.time()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return payload + "~S"
        return self.ERROR_PREFIX + self._b64("e")


# ── 默认 User-Agent 和 sec-ch-ua ──────────────────────────────
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
    "Gecko/20100101 Firefox/145.0"
)
DEFAULT_SENTINEL_SEC_CH_UA = ""

_SDK_CACHE_LOCK = threading.Lock()
_SDK_SOURCE_CACHE = ""
_VM_RUNNER = Path(__file__).with_name("sentinel_vm_runner.cjs")


def _load_sdk_source(session: "Session", user_agent: str, sec_ch_ua: str) -> str:
    global _SDK_SOURCE_CACHE
    with _SDK_CACHE_LOCK:
        if _SDK_SOURCE_CACHE:
            return _SDK_SOURCE_CACHE
        headers = {
            "Accept": "*/*",
            "Referer": "https://auth.openai.com/",
            "User-Agent": user_agent,
        }
        if sec_ch_ua:
            headers.update(
                {
                    "sec-ch-ua": sec_ch_ua,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }
            )
        resp = session.get(
            SentinelTokenGenerator.SCRIPT_URL,
            headers=headers,
            timeout=30,
            verify=True,
        )
        source = str(resp.text or "")
        if resp.status_code != 200 or "SentinelSDK" not in source:
            raise RuntimeError(f"sentinel_sdk_fetch_failed_{resp.status_code}")
        _SDK_SOURCE_CACHE = source
        return source


def _run_vm_proofs(
    session: "Session",
    chat_req: dict,
    cached_proof: str,
    flow: str,
    device_id: str,
    user_agent: str,
    sec_ch_ua: str,
) -> dict[str, str]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("sentinel_vm_node_not_found")
    if not _VM_RUNNER.is_file():
        raise RuntimeError("sentinel_vm_runner_not_found")
    payload = {
        "cachedProof": cached_proof,
        "chatReq": chat_req,
        "deviceId": device_id,
        "flow": flow,
        "pageUrl": "https://auth.openai.com/about-you" if flow == "oauth_create_account" else "https://auth.openai.com/create-account/password",
        "sdkSource": _load_sdk_source(session, user_agent, sec_ch_ua),
        "sdkUrl": SentinelTokenGenerator.SCRIPT_URL,
        "userAgent": user_agent,
    }
    result = subprocess.run(
        [node, str(_VM_RUNNER)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sentinel_vm_failed: {result.stderr.strip()[:500]}")
    try:
        parsed = json.loads(result.stdout or "{}")
    except Exception as error:
        raise RuntimeError("sentinel_vm_invalid_output") from error
    return {"t": str(parsed.get("t") or ""), "so": str(parsed.get("so") or "")}


def build_sentinel_headers(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> dict[str, str]:
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(device_id, ua)
    requirements_proof = generator.generate_requirements_token()
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": ua,
    }
    if ch_ua:
        headers.update(
            {
                "sec-ch-ua": ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": requirements_proof, "id": device_id, "flow": flow}),
        headers=headers,
        timeout=20,
        verify=True,
    )
    try:
        data = resp.json() if resp.text else {}
    except Exception as error:
        raise RuntimeError("sentinel_req_invalid_response") from error
    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    turnstile = data.get("turnstile") if isinstance(data.get("turnstile"), dict) else {}
    observer = data.get("so") if isinstance(data.get("so"), dict) else {}
    needs_t = bool(turnstile.get("required") or turnstile.get("dx"))
    needs_so = bool(observer.get("required"))
    proofs = _run_vm_proofs(session, data, requirements_proof, flow, device_id, ua, ch_ua) if needs_t or needs_so else {"t": "", "so": ""}
    if needs_t and not proofs.get("t"):
        raise RuntimeError(f"sentinel_turnstile_missing_for_{flow}")
    if needs_so and not proofs.get("so"):
        raise RuntimeError(f"sentinel_session_observer_missing_for_{flow}")
    sentinel_value = json.dumps(
        {"p": p_value, "t": proofs.get("t") or None, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = {"openai-sentinel-token": sentinel_value, "oai-sc": "0" + token}
    if proofs.get("so"):
        result["openai-sentinel-so-token"] = json.dumps(
            {"so": proofs["so"], "c": token, "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
    return result


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    headers = build_sentinel_headers(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
    )
    return headers["openai-sentinel-token"], headers.get("oai-sc", "")
