"""Encrypted local storage for reusable PayPal browser/HTTP sessions."""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / ".runtime" / "br-new" / "sessions"
SESSION_MODE_DIRS = {
    "BR": ROOT / ".runtime" / "br" / "sessions",
    "BR_NEW": SESSION_DIR,
    "GB": ROOT / ".runtime" / "gb" / "sessions",
    "GB_NEW": ROOT / ".runtime" / "gb-new" / "sessions",
    "US": ROOT / ".runtime" / "us" / "sessions",
    "US_NEW": ROOT / ".runtime" / "us-new" / "sessions",
    "TH_NEW": ROOT / ".runtime" / "th-new" / "sessions",
    "ID_NEW": ROOT / ".runtime" / "id-new" / "sessions",
}
SNAPSHOT_VERSION = 1
_DPAPI_DESCRIPTION = "paypal-br-new-session-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SessionSnapshotError(RuntimeError):
    pass


def _refresh_flow_runtime_profile(flow: Any) -> None:
    session = getattr(flow, "session", None)
    refresh_runtime_profile = getattr(session, "refresh_runtime_profile", None)
    if callable(refresh_runtime_profile):
        refresh_runtime_profile()
        return

    module_name = str(getattr(type(session), "__module__", "") or "")
    if not module_name:
        return
    try:
        session_module = importlib.import_module(module_name)
    except (ImportError, ValueError):
        return
    build_common_headers = getattr(session_module, "build_common_headers", None)
    build_high_entropy_hints = getattr(session_module, "build_high_entropy_hints", None)
    if callable(build_high_entropy_hints):
        session._high_entropy_hints = build_high_entropy_hints(flow.state)
    if not callable(build_common_headers):
        return
    headers = build_common_headers(flow.state)
    client_headers = getattr(getattr(session, "client", None), "headers", None)
    if client_headers is None:
        return
    try:
        client_headers.update(headers)
    except Exception:
        for name, value in headers.items():
            try:
                client_headers[name] = value
            except Exception:
                pass


def normalize_session_mode(mode: str) -> str:
    normalized = str(mode or "BR_NEW").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized not in SESSION_MODE_DIRS:
        raise SessionSnapshotError(f"Unsupported session mode: {mode}")
    return normalized


def session_directory(mode: str) -> Path:
    return SESSION_MODE_DIRS[normalize_session_mode(mode)]


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise SessionSnapshotError("BR New session snapshots require Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _input_blob(data)
    destination = _DataBlob()
    description = _DPAPI_DESCRIPTION if protect else None
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description_out = ctypes.c_wchar_p()
    if protect:
        ok = function(
            ctypes.byref(source),
            description,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(destination),
        )
    else:
        ok = function(
            ctypes.byref(source),
            ctypes.byref(description_out),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(destination),
        )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)
        if description_out:
            kernel32.LocalFree(description_out)


def protect_snapshot(payload: dict[str, Any]) -> bytes:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _dpapi_transform(serialized, protect=True)


def unprotect_snapshot(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(_dpapi_transform(data, protect=False).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionSnapshotError("BR New session snapshot is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != SNAPSHOT_VERSION:
        raise SessionSnapshotError("Unsupported BR New session snapshot version")
    return payload


def _cookie_payload(flow: Any) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    jar = getattr(getattr(getattr(flow, "session", None), "client", None), "cookies", None)
    for cookie in getattr(jar, "jar", []) or []:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        if not name or not value:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": str(getattr(cookie, "domain", "") or ".paypal.com"),
            "path": str(getattr(cookie, "path", "") or "/"),
            "secure": bool(getattr(cookie, "secure", True)),
            "expires": int(getattr(cookie, "expires", 0) or 0),
        })
    return cookies


def snapshot_from_flow(
    flow: Any,
    *,
    proxy_url: str = "",
    mode: str = "BR_NEW",
    country: str = "",
) -> dict[str, Any]:
    mode = normalize_session_mode(mode)
    state = flow.state
    captured_at = time.time()
    state_fields = {
        key: getattr(state, key, None)
        for key in (
            "user_id",
            "ec_token",
            "paypal_client_metadata_id",
            "euat_token",
            "nsid",
            "d_id",
            "datadome_cookie",
            "datadome_clientid",
            "tltsid",
            "tltdid",
            "browser_profile",
            "screen",
            "viewport",
            "device_fingerprint",
            "pxp_guid",
            "fpti_calc",
        )
    }
    source = f"{getattr(state, 'user_id', '')}:{captured_at}:{getattr(flow, 'ba_token', '')}"
    return {
        "version": SNAPSHOT_VERSION,
        "mode": mode,
        "country": str(country or getattr(getattr(flow, "address", None), "country", "") or mode.split("_", 1)[0]).upper(),
        "captured_at": captured_at,
        "snapshot_id": hashlib.sha256(source.encode("utf-8")).hexdigest()[:16],
        "source_ba_hash": hashlib.sha256(
            str(getattr(flow, "ba_token", "")).encode("utf-8")
        ).hexdigest(),
        "proxy_url": proxy_url,
        "state": state_fields,
        "cookies": _cookie_payload(flow),
    }


def _snapshot_is_usable(payload: dict[str, Any]) -> bool:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    return bool(
        state.get("user_id")
        and state.get("euat_token")
        and state.get("ec_token")
        and payload.get("cookies")
    )


def save_session_snapshot(payload: dict[str, Any], directory: Path = SESSION_DIR) -> Path:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    if not state.get("user_id"):
        raise SessionSnapshotError("Successful session did not contain a buyer user ID")
    if not state.get("euat_token"):
        raise SessionSnapshotError("Successful session did not contain an EUAT token")
    if not state.get("ec_token"):
        raise SessionSnapshotError("Successful session did not contain an EC token")
    if not payload.get("cookies"):
        raise SessionSnapshotError("Successful session did not contain cookies")
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(payload["captured_at"]))
    snapshot_id = str(payload.get("snapshot_id") or "unknown")
    path = directory / f"session-{timestamp}-{snapshot_id}.dpapi"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(protect_snapshot(payload))
    temporary.replace(path)
    return path


def load_session_snapshot(path: Path) -> dict[str, Any]:
    return unprotect_snapshot(path.read_bytes())


def latest_session_snapshot(directory: Path = SESSION_DIR) -> tuple[Path, dict[str, Any]]:
    paths = sorted(directory.glob("session-*.dpapi"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not paths:
        raise SessionSnapshotError("No successful BR New session snapshot is available")
    return paths[0], load_session_snapshot(paths[0])


def latest_mode_session_snapshot(mode: str) -> tuple[Path, dict[str, Any]]:
    normalized_mode = normalize_session_mode(mode)
    directory = session_directory(normalized_mode)
    paths = sorted(
        directory.glob("session-*.dpapi"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            payload = load_session_snapshot(path)
        except (OSError, SessionSnapshotError):
            continue
        payload_mode = str(payload.get("mode") or "BR_NEW").upper()
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        if payload_mode != normalized_mode:
            continue
        if not _snapshot_is_usable(payload):
            continue
        return path, payload
    raise SessionSnapshotError(
        f"No usable {normalized_mode} session containing Cookie, EUAT, buyer and EC is available"
    )


def session_snapshot_status(mode: str) -> dict[str, Any]:
    normalized_mode = normalize_session_mode(mode)
    try:
        path, payload = latest_mode_session_snapshot(normalized_mode)
    except SessionSnapshotError:
        return {"mode": normalized_mode, "available": False}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    return {
        "mode": normalized_mode,
        "country": payload.get("country") or normalized_mode.split("_", 1)[0],
        "available": _snapshot_is_usable(payload),
        "snapshot_id": payload.get("snapshot_id"),
        "captured_at": payload.get("captured_at"),
        "cookie_count": len(payload.get("cookies") or []),
        "file": path.name,
    }


def restore_flow_session(flow: Any, payload: dict[str, Any]) -> int:
    state_payload = payload.get("state")
    if not isinstance(state_payload, dict):
        raise SessionSnapshotError("BR New session snapshot has no state")
    for key, value in state_payload.items():
        if hasattr(flow.state, key) and value is not None:
            setattr(flow.state, key, value)

    _refresh_flow_runtime_profile(flow)

    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        raise SessionSnapshotError("BR New session snapshot has no cookie jar")
    valid_cookies = [
        cookie
        for cookie in cookies
        if isinstance(cookie, dict)
        and str(cookie.get("name") or "")
        and str(cookie.get("value") or "")
    ]
    import_browser_cookies = getattr(flow.session, "import_browser_cookies", None)
    if callable(import_browser_cookies):
        import_browser_cookies(valid_cookies)
        restored = len(valid_cookies)
    else:
        restored = 0
        for cookie in valid_cookies:
            flow.session.client.cookies.set(
                str(cookie.get("name") or ""),
                str(cookie.get("value") or ""),
                domain=str(cookie.get("domain") or ".paypal.com"),
                path=str(cookie.get("path") or "/"),
            )
            restored += 1
    if hasattr(flow.session, "_sync_state_cookies"):
        flow.session._sync_state_cookies()
    return restored


def discard_stale_datadome_state(flow: Any) -> bool:
    """Drop only the short-lived DataDome cookie before opening a new BA."""
    state = getattr(flow, "state", None)
    for name, value in (
        ("datadome_cookie", ""),
        ("datadome_clientid", ""),
        ("datadome_browser_solved", False),
        ("datadome_browser_result", {}),
    ):
        if state is not None and hasattr(state, name):
            setattr(state, name, value)

    jar = getattr(getattr(getattr(flow, "session", None), "client", None), "cookies", None)
    if jar is None:
        return False
    try:
        jar.delete("datadome")
        return True
    except Exception:
        pass
    raw_jar = getattr(jar, "jar", None)
    if raw_jar is None:
        return False
    removed = False
    for cookie in list(raw_jar):
        if str(getattr(cookie, "name", "") or "") != "datadome":
            continue
        try:
            raw_jar.clear(
                domain=getattr(cookie, "domain", None),
                path=getattr(cookie, "path", None),
                name="datadome",
            )
            removed = True
        except Exception:
            pass
    return removed
