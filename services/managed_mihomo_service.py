from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import json
import os
import platform
import random
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import psutil
from curl_cffi import requests as curl_requests

from services.atomic_json_store import JsonStoreReadError, read_json, write_json
from services.config import DATA_DIR
from services import proxy_pool_store
from services.proxy_service import normalize_proxy_url


RUNTIME_ROOT = DATA_DIR / "managed_mihomo"
DEFAULT_BINARY = RUNTIME_ROOT / "mihomo.exe"
RELEASE_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
SELECTOR = "REGISTER"
METADATA_NODE_MARKERS = (
    "剩余流量",
    "流量剩余",
    "距离下次重置",
    "下次重置",
    "套餐到期",
    "到期时间",
    "订阅到期",
    "官网地址",
    "官方网站",
)


def _is_metadata_node(name: str) -> bool:
    text = str(name or "").strip().lower()
    return not text or any(marker.lower() in text for marker in METADATA_NODE_MARKERS)


def _request_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 15, secret: str = "") -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "MoyuRegister/1.0"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(data or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _decode_subscription(text: str) -> str:
    raw = str(text or "").strip()
    if not raw or "\n" in raw or "proxies:" in raw or "payload:" in raw:
        return raw
    compact = "".join(raw.split())
    try:
        decoded = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", errors="replace")
        if "\n" in decoded or "://" in decoded:
            return decoded
    except Exception:
        pass
    return raw


def _fetch_subscription(url: str, *, allow_private: bool = False) -> str:
    return _decode_subscription(proxy_pool_store._fetch_text(url, allow_private=allow_private))


def _subscription_proxies(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    subscription.pop("_managed_dns", None)
    text = _fetch_subscription(
        str(subscription.get("url") or ""),
        allow_private=bool(subscription.get("allow_private")),
    )
    try:
        parsed = yaml.safe_load(text)
    except Exception as error:
        raise RuntimeError(f"订阅 {subscription.get('name') or subscription.get('id')} 不是有效的 Clash/Mihomo 配置：{error}") from error
    if isinstance(parsed, dict):
        raw_proxies = parsed.get("proxies") if isinstance(parsed.get("proxies"), list) else parsed.get("payload")
        if isinstance(parsed.get("dns"), dict):
            source_dns = dict(parsed["dns"])
            nameservers = [str(item) for item in source_dns.get("nameserver") or [] if str(item).strip()]
            default_nameservers = [str(item) for item in source_dns.get("default-nameserver") or [] if str(item).strip()]
            subscription["_managed_dns"] = {
                "enable": True,
                "ipv6": bool(source_dns.get("ipv6", False)),
                "enhanced-mode": "redir-host",
                "default-nameserver": default_nameservers or ["223.5.5.5", "119.29.29.29"],
                "nameserver": nameservers or ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
                "proxy-server-nameserver": nameservers or ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
            }
    else:
        raw_proxies = None
    if not isinstance(raw_proxies, list):
        if "://" in text:
            raise RuntimeError(f"订阅 {subscription.get('name') or subscription.get('id')} 返回通用 URI 列表，请在机场订阅地址中选择 Clash.Meta 格式")
        raise RuntimeError(f"订阅 {subscription.get('name') or subscription.get('id')} 没有 proxies/payload 节点列表")
    return [dict(item) for item in raw_proxies if isinstance(item, dict) and str(item.get("name") or "").strip()]


def _merged_proxy_config(subscriptions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    proxies: list[dict[str, Any]] = []
    names: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    dns_config: dict[str, Any] = {}
    subscription_items: list[tuple[str, list[dict[str, Any]]]] = []
    for subscription in subscriptions:
        try:
            items = _subscription_proxies(subscription)
            if isinstance(subscription.get("_managed_dns"), dict):
                dns_config = dict(subscription["_managed_dns"])
        except Exception as error:
            errors.append(str(error))
            continue
        prefix = str(subscription.get("name") or subscription.get("id") or "订阅").strip()
        subscription_items.append((prefix, items))
    max_items = max((len(items) for _, items in subscription_items), default=0)
    for index in range(max_items):
        for prefix, items in subscription_items:
            if index >= len(items):
                continue
            raw = dict(items[index])
            name = str(raw.get("name") or "").strip()
            if _is_metadata_node(name):
                continue
            unique_name = f"[{prefix}] {name}"
            if unique_name in seen:
                unique_name = f"{prefix} · {name}"
            suffix = 2
            while unique_name in seen:
                unique_name = f"{prefix} · {name} ({suffix})"
                suffix += 1
            raw["name"] = unique_name
            proxies.append(raw)
            names.append(unique_name)
            seen.add(unique_name)
    if not proxies:
        detail = "；".join(errors[:3]) if errors else "没有启用且可读取的订阅"
        raise RuntimeError(f"托管 Mihomo 未读取到可用节点：{detail}")
    return proxies, names, dns_config


def _asset_priority(name: str) -> tuple[int, int]:
    lowered = name.lower()
    if "windows" not in lowered or not lowered.endswith((".zip", ".gz")):
        return (99, 99)
    machine = platform.machine().lower()
    if "arm" in machine:
        return (0 if "arm64" in lowered else 99, 0 if lowered.endswith(".zip") else 1)
    if "amd64-v3" in lowered:
        return (0, 0 if lowered.endswith(".zip") else 1)
    if "amd64-compatible" in lowered:
        return (1, 0 if lowered.endswith(".zip") else 1)
    if "amd64" in lowered and "go120" not in lowered:
        return (2, 0 if lowered.endswith(".zip") else 1)
    return (99, 99)


def _download_binary(destination: Path) -> Path:
    release = _request_json(RELEASE_API, timeout=30)
    assets = [asset for asset in release.get("assets") or [] if isinstance(asset, dict)]
    candidates = sorted(
        [asset for asset in assets if _asset_priority(str(asset.get("name") or ""))[0] < 99],
        key=lambda asset: _asset_priority(str(asset.get("name") or "")),
    )
    if not candidates:
        raise RuntimeError("官方 Mihomo 最新版本中没有可用的 Windows 核心")
    asset = candidates[0]
    url = str(asset.get("browser_download_url") or "")
    name = str(asset.get("name") or "mihomo.zip")
    digest = str(asset.get("digest") or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("Mihomo release asset is missing a SHA-256 digest")
    if not url:
        raise RuntimeError("Mihomo 下载地址为空")
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / name
    request = urllib.request.Request(url, headers={"User-Agent": "MoyuRegister/1.0"}, method="GET")
    with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as output:
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > 128 * 1024 * 1024:
                raise RuntimeError("Mihomo archive exceeds 128 MiB")
            output.write(chunk)
    actual_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not secrets.compare_digest(actual_digest, digest.split(":", 1)[1]):
        raise RuntimeError("Mihomo archive SHA-256 verification failed")
    temporary = destination.with_suffix(".tmp.exe")
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            executable = next((item for item in bundle.infolist() if item.filename.lower().endswith(".exe")), None)
            if executable is None:
                raise RuntimeError("Mihomo 压缩包中没有 exe")
            with bundle.open(executable) as source, temporary.open("wb") as output:
                output.write(source.read())
    else:
        with gzip.open(archive, "rb") as source, temporary.open("wb") as output:
            output.write(source.read())
    os.replace(temporary, destination)
    try:
        archive.unlink()
    except Exception:
        pass
    return destination


def _managed_executable_path(value: str = "") -> Path:
    root = RUNTIME_ROOT.resolve()
    candidate = Path(str(value or "").strip()) if str(value or "").strip() else root / "mihomo.exe"
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError("Mihomo executable must stay inside the managed runtime directory") from error
    if resolved.name.casefold() != "mihomo.exe":
        raise RuntimeError("Managed Mihomo executable must be named mihomo.exe")
    return resolved


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@dataclass
class _ManagedInstance:
    slot: int
    proxy_port: int
    api_port: int
    root: Path
    process: subprocess.Popen[Any]
    log_handle: Any
    fingerprint: str
    selected: str = ""

    @property
    def proxy_url(self) -> str:
        return normalize_proxy_url(f"http://127.0.0.1:{self.proxy_port}")

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def running(self) -> bool:
        return self.process.poll() is None


class ManagedMihomoManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[int, _ManagedInstance] = {}
        self._purpose_slots: dict[tuple[str, int], int] = {}
        self._health_cache: dict[str, tuple[float, int]] = {}
        self._probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._quarantine: dict[str, tuple[float, str]] = {}
        self._last_error = ""
        self._last_started_at = ""

    def install(self, executable: str = "") -> dict[str, Any]:
        path = _managed_executable_path(executable)
        if path.is_file():
            return {"ok": True, "installed": True, "executable": str(path)}
        installed = _download_binary(path)
        return {"ok": True, "installed": True, "executable": str(installed)}

    def start(self, total_threads: int, store: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_locked(max(1, int(total_threads or 1)), store)
            return self.status()

    def stop(self, *, include_persisted: bool = True) -> dict[str, Any]:
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
            self._purpose_slots.clear()
            for instance in instances:
                try:
                    if instance.running():
                        instance.process.kill() if os.name == "nt" else instance.process.terminate()
                except Exception:
                    pass
            for instance in instances:
                try:
                    instance.process.wait(timeout=2)
                except Exception:
                    try:
                        instance.process.kill()
                    except Exception:
                        pass
                try:
                    instance.log_handle.close()
                except Exception:
                    pass
                self._remove_instance_record(instance.root)
            if include_persisted:
                self._stop_persisted_instances_locked()
            return self.status()

    def stop_owned(self) -> dict[str, Any]:
        return self.stop(include_persisted=False)

    def status(self) -> dict[str, Any]:
        with self._lock:
            executable = DEFAULT_BINARY
            quarantined = self._active_quarantine_locked()
            instances = []
            for slot, instance in sorted(self._instances.items()):
                selected_probe = {}
                if instance.selected:
                    cached_probe = self._probe_cache.get(f"{instance.fingerprint}:{instance.selected}")
                    selected_probe = dict(cached_probe[1]) if cached_probe else {}
                instances.append({
                    "slot": slot + 1,
                    "running": instance.running(),
                    "pid": instance.process.pid,
                    "proxy_url": instance.proxy_url,
                    "api_base": instance.api_base,
                    "selected": instance.selected,
                    "region": selected_probe.get("region", ""),
                    "openai_status": selected_probe.get("openai_status", 0),
                    "probe_latency_ms": selected_probe.get("latency_ms", 0),
                })
            return {
                "installed": executable.is_file(),
                "default_executable": str(executable),
                "running": sum(1 for item in instances if item["running"]),
                "instances": instances,
                "last_error": self._last_error,
                "last_started_at": self._last_started_at,
                "quarantined": [
                    {
                        "name": name,
                        "until": until,
                        "remaining_seconds": max(0, int(until - time.time())),
                        "reason": reason,
                    }
                    for name, (until, reason) in sorted(quarantined.items())
                ],
            }

    def report_failure(self, assignment: dict[str, Any], reason: object, quarantine_seconds: int = 900) -> dict[str, Any]:
        if not isinstance(assignment, dict) or str(assignment.get("source") or "") != "managed_mihomo":
            return {"ok": False, "message": "不是托管 Mihomo 分配"}
        node = assignment.get("node") if isinstance(assignment.get("node"), dict) else {}
        name = str(assignment.get("label") or node.get("name") or "").strip()
        if not name:
            return {"ok": False, "message": "缺少节点名称"}
        seconds = max(60, min(3600, int(quarantine_seconds or 900)))
        with self._lock:
            until = self._quarantine_node_locked(name, str(reason or "运行时连接失败"), seconds)
            return {
                "ok": True,
                "name": name,
                "until": until,
                "remaining_seconds": seconds,
            }

    def assign(
        self,
        *,
        slot: int,
        task_index: int,
        total_threads: int,
        store: dict[str, Any],
        purpose: str = "",
        name_filter: str = "",
        exact_node_name: str = "",
        validate_node: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            logical_slot = max(0, int(slot or 0))
            physical_slot = self._physical_slot_locked(purpose, logical_slot)
            self._ensure_locked(max(1, int(total_threads or 1), physical_slot + 1), store)
            instance = self._instances.get(physical_slot)
            if instance is None or not instance.running():
                raise RuntimeError(f"托管 Mihomo 线程 {slot + 1} 未运行")
            exact_name = str(exact_node_name or "").strip()
            candidates = self._candidate_names(instance, store, name_filter=exact_name or name_filter)
            if exact_name:
                candidates = [name for name in candidates if name == exact_name]
            if not candidates:
                if exact_name:
                    raise RuntimeError(f"注册节点不可用: {exact_name}")
                raise RuntimeError("托管 Mihomo 没有可分配节点")
            mode = str(store.get("mode") or "per_task")
            strategy = str(store.get("strategy") or "round_robin")
            base_index = max(0, int(task_index or 1) - 1) if mode == "per_task" else logical_slot
            if exact_name:
                candidates = [exact_name]
            elif strategy == "random":
                random.shuffle(candidates)
            else:
                offset = base_index % len(candidates)
                candidates = candidates[offset:] + candidates[:offset]
            retries = 1 if exact_name else min(len(candidates), max(1, int(store.get("max_retry_count") or 3)))
            last_error = ""
            for name in candidates[:retries]:
                try:
                    latency_ms = self._node_delay(instance, name) if validate_node else 0
                    _request_json(
                        f"{instance.api_base}/proxies/{urllib.parse.quote(SELECTOR, safe='')}",
                        method="PUT",
                        body={"name": name},
                    )
                    instance.selected = name
                    probe = self._probe_selected_proxy(instance, name) if validate_node else {}
                    return {
                        "proxy": instance.proxy_url,
                        "source": "managed_mihomo",
                        "node_id": hashlib.sha1(name.encode("utf-8", errors="ignore")).hexdigest()[:16],
                        "label": name,
                        "slot": logical_slot + 1,
                        "managed_slot": physical_slot + 1,
                        "purpose": str(purpose or ""),
                        "node": {"name": name, "type": "managed_mihomo"},
                        "sticky": bool(exact_name),
                        "latency_ms": latency_ms,
                        "probe_latency_ms": int(probe.get("latency_ms") or 0),
                        "region": str(probe.get("region") or ""),
                        "openai_status": int(probe.get("openai_status") or 0),
                    }
                except Exception as error:
                    last_error = str(error)
                    if validate_node:
                        self._quarantine_node_locked(name, last_error, 300)
                    delay = float(store.get("retry_delay") or 0)
                    if delay > 0:
                        time.sleep(delay)
            raise RuntimeError(f"托管 Mihomo 节点切换失败：{last_error or '未知错误'}")

    def _physical_slot_locked(self, purpose: str, logical_slot: int) -> int:
        key = (str(purpose or "default").strip().lower() or "default", max(0, int(logical_slot or 0)))
        existing = self._purpose_slots.get(key)
        if existing is not None:
            return existing
        used = set(self._purpose_slots.values())
        preferred = key[1]
        physical_slot = preferred if preferred not in used else 0
        while physical_slot in used:
            physical_slot += 1
        self._purpose_slots[key] = physical_slot
        return physical_slot

    def _node_delay(self, instance: _ManagedInstance, name: str) -> int:
        key = f"{instance.fingerprint}:{name}"
        cached = self._health_cache.get(key)
        if cached and time.time() - cached[0] < (300 if cached[1] > 0 else 30):
            if cached[1] <= 0:
                raise RuntimeError("节点最近 HTTPS 测活失败")
            return cached[1]
        query = urllib.parse.urlencode({
            "timeout": 8000,
            "url": "https://www.gstatic.com/generate_204",
        })
        try:
            data = _request_json(
                f"{instance.api_base}/proxies/{urllib.parse.quote(name, safe='')}/delay?{query}",
                timeout=12,
            )
            delay = int(data.get("delay") or 0)
            if delay <= 0:
                raise RuntimeError("节点延迟测试未返回有效结果")
            self._health_cache[key] = (time.time(), delay)
            return delay
        except Exception:
            self._health_cache[key] = (time.time(), 0)
            raise

    def _probe_selected_proxy(self, instance: _ManagedInstance, name: str) -> dict[str, Any]:
        key = f"{instance.fingerprint}:{name}"
        cached = self._probe_cache.get(key)
        if cached and time.time() - cached[0] < 120:
            return dict(cached[1])
        common = {
            "proxy": instance.proxy_url,
            "timeout": 15,
            "impersonate": "chrome",
            "verify": False,
            "allow_redirects": False,
        }
        started_at = time.time()
        trace = curl_requests.get("https://cloudflare.com/cdn-cgi/trace", **common)
        if int(trace.status_code or 0) != 200:
            raise RuntimeError(f"节点地区预检失败：HTTP {trace.status_code}")
        region_match = re.search(r"^loc=(.+)$", str(trace.text or ""), re.MULTILINE)
        region = str(region_match.group(1) if region_match else "").strip().upper()
        if region in {"CN", "HK"}:
            raise RuntimeError(f"节点地区不支持 OpenAI：{region}")
        latency_ms = max(1, int((time.time() - started_at) * 1000))
        target = curl_requests.get("https://auth.openai.com/", **common)
        status = int(target.status_code or 0)
        if status in {403, 451} or status >= 500 or status <= 0:
            raise RuntimeError(f"OpenAI 目标站预检失败：HTTP {status}")
        result = {
            "region": region or "UNKNOWN",
            "latency_ms": latency_ms,
            "openai_status": status,
        }
        self._probe_cache[key] = (time.time(), result)
        return dict(result)

    def _candidate_names(self, instance: _ManagedInstance, store: dict[str, Any], *, name_filter: str = "") -> list[str]:
        deadline = time.time() + max(5, int((store.get("managed_mihomo") or {}).get("startup_timeout") or 30))
        last_error = ""
        while time.time() < deadline:
            try:
                data = _request_json(f"{instance.api_base}/proxies", timeout=5)
                proxies = data.get("proxies") if isinstance(data.get("proxies"), dict) else {}
                group = proxies.get(SELECTOR) if isinstance(proxies.get(SELECTOR), dict) else {}
                names = [str(name) for name in group.get("all") or [] if not _is_metadata_node(str(name))]
                normalized_filter = str(name_filter or "").strip().lower()
                if normalized_filter:
                    names = [name for name in names if normalized_filter in name.lower()]
                blacklist = [str(item).strip().lower() for item in store.get("node_blacklist") or [] if str(item).strip()]
                names = [name for name in names if not any(term in name.lower() for term in blacklist)]
                quarantined = self._active_quarantine_locked()
                names = [name for name in names if name not in quarantined]
                if bool(store.get("fastest_mode")):
                    def delay_for(name: str) -> int:
                        item = proxies.get(name) if isinstance(proxies.get(name), dict) else {}
                        history = item.get("history") if isinstance(item.get("history"), list) else []
                        delay = int((history[-1] if history else {}).get("delay") or 10**9) if history else 10**9
                        return delay if delay > 0 else 10**9
                    names.sort(key=delay_for)
                if names:
                    return names
            except Exception as error:
                last_error = str(error)
            time.sleep(0.5)
        raise RuntimeError(f"托管 Mihomo 节点尚未就绪：{last_error or '订阅可能仍在加载'}")

    def _active_quarantine_locked(self) -> dict[str, tuple[float, str]]:
        now = time.time()
        for name, (until, _reason) in list(self._quarantine.items()):
            if until <= now:
                self._quarantine.pop(name, None)
        return dict(self._quarantine)

    def _quarantine_node_locked(self, name: str, reason: str, seconds: int) -> float:
        until = time.time() + max(60, int(seconds or 300))
        compact_reason = " ".join(str(reason or "节点连接失败").split())[:300]
        current = self._quarantine.get(name)
        if current and current[0] > until:
            until = current[0]
        self._quarantine[name] = (until, compact_reason)
        suffix = f":{name}"
        for key in [item for item in self._health_cache if item.endswith(suffix)]:
            self._health_cache.pop(key, None)
        for key in [item for item in self._probe_cache if item.endswith(suffix)]:
            self._probe_cache.pop(key, None)
        return until

    def _ensure_locked(self, total_threads: int, store: dict[str, Any]) -> None:
        live_pids = {instance.process.pid for instance in self._instances.values() if instance.running()}
        self._stop_persisted_instances_locked(exclude_pids=live_pids)
        config = store.get("managed_mihomo") if isinstance(store.get("managed_mihomo"), dict) else {}
        if not bool(config.get("enabled")):
            raise RuntimeError("请先启用项目托管 Mihomo")
        executable = _managed_executable_path(str(config.get("executable") or ""))
        if not executable.is_file():
            if not bool(config.get("auto_download", True)):
                raise RuntimeError(f"Mihomo 核心不存在：{executable}")
            executable = Path(self.install(str(executable))["executable"])
        subscriptions = [item for item in store.get("subscriptions") or [] if isinstance(item, dict) and item.get("enabled") and str(item.get("url") or "").strip()]
        if not subscriptions:
            raise RuntimeError("没有启用的代理订阅")
        fingerprint = hashlib.sha256(json.dumps({
            "subscriptions": [(item.get("id"), item.get("url"), item.get("updated_at")) for item in subscriptions],
            "executable": str(executable),
            "proxy_port_base": config.get("proxy_port_base"),
            "api_port_base": config.get("api_port_base"),
            "config_version": 7,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest()
        active = [item for item in self._instances.values() if item.running() and item.fingerprint == fingerprint]
        if len(active) < total_threads:
            proxies, names, dns_config = _merged_proxy_config(subscriptions)
        else:
            proxies, names, dns_config = [], [], {}
        for slot in list(self._instances):
            if not self._instances[slot].running() or self._instances[slot].fingerprint != fingerprint:
                self._stop_slot_locked(slot)
        for slot in range(total_threads):
            if slot in self._instances and self._instances[slot].running():
                continue
            if not proxies:
                proxies, names, dns_config = _merged_proxy_config(subscriptions)
            self._start_slot_locked(slot, executable, config, proxies, names, dns_config, fingerprint)
        self._last_error = ""
        self._last_started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def _start_slot_locked(
        self,
        slot: int,
        executable: Path,
        config: dict[str, Any],
        proxies: list[dict[str, Any]],
        names: list[str],
        dns_config: dict[str, Any],
        fingerprint: str,
    ) -> None:
        proxy_port = int(config.get("proxy_port_base") or 42000) + slot
        api_port = int(config.get("api_port_base") or 43000) + slot
        if not _port_available("127.0.0.1", proxy_port):
            raise RuntimeError(f"托管 Mihomo 代理端口 {proxy_port} 已被占用")
        if not _port_available("127.0.0.1", api_port):
            raise RuntimeError(f"托管 Mihomo API 端口 {api_port} 已被占用")
        root = RUNTIME_ROOT / f"thread-{slot + 1}"
        root.mkdir(parents=True, exist_ok=True)
        runtime_config = {
            "mixed-port": proxy_port,
            "external-controller": f"127.0.0.1:{api_port}",
            "secret": "",
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "ipv6": False,
            "unified-delay": True,
            "tcp-concurrent": True,
            "profile": {"store-selected": False, "store-fake-ip": False},
            "proxies": proxies,
            "proxy-groups": [{"name": SELECTOR, "type": "select", "proxies": names}],
            "rules": [f"MATCH,{SELECTOR}"],
        }
        if dns_config:
            runtime_config["dns"] = dns_config
        config_path = root / "config.yaml"
        config_path.write_text(yaml.safe_dump(runtime_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        log_handle = (root / "mihomo.log").open("ab")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(executable), "-d", str(root), "-f", str(config_path)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        instance = _ManagedInstance(slot, proxy_port, api_port, root, process, log_handle, fingerprint)
        self._instances[slot] = instance
        self._write_instance_record(instance, executable)
        deadline = time.time() + max(5, int(config.get("startup_timeout") or 30))
        while time.time() < deadline:
            if not instance.running():
                self._last_error = (root / "mihomo.log").read_text(encoding="utf-8", errors="replace")[-1000:]
                self._stop_slot_locked(slot)
                raise RuntimeError(f"托管 Mihomo 线程 {slot + 1} 启动失败：{self._last_error or '进程已退出'}")
            try:
                _request_json(f"{instance.api_base}/version", timeout=2)
                return
            except Exception:
                time.sleep(0.25)
        self._stop_slot_locked(slot)
        raise RuntimeError(f"托管 Mihomo 线程 {slot + 1} 启动超时")

    def _stop_slot_locked(self, slot: int) -> None:
        instance = self._instances.pop(slot, None)
        if not instance:
            return
        try:
            if instance.running():
                if os.name == "nt":
                    instance.process.kill()
                else:
                    instance.process.terminate()
                instance.process.wait(timeout=2)
        except Exception:
            try:
                instance.process.kill()
            except Exception:
                pass
        try:
            instance.log_handle.close()
        except Exception:
            pass
        self._remove_instance_record(instance.root)

    @staticmethod
    def _record_path(root: Path) -> Path:
        return root / "managed-instance.json"

    @staticmethod
    def _same_path(left: str | Path, right: str | Path) -> bool:
        try:
            left_value = str(Path(left).resolve())
            right_value = str(Path(right).resolve())
        except Exception:
            return False
        if os.name == "nt":
            return left_value.casefold() == right_value.casefold()
        return left_value == right_value

    def _write_instance_record(self, instance: _ManagedInstance, executable: Path) -> None:
        try:
            create_time = float(psutil.Process(instance.process.pid).create_time())
            write_json(
                self._record_path(instance.root),
                {
                    "pid": instance.process.pid,
                    "create_time": create_time,
                    "executable": str(executable.resolve()),
                    "root": str(instance.root.resolve()),
                    "config": str((instance.root / "config.yaml").resolve()),
                    "proxy_port": instance.proxy_port,
                    "api_port": instance.api_port,
                },
                schema_version=1,
            )
        except Exception as error:
            self._stop_slot_locked(instance.slot)
            raise RuntimeError(f"托管 Mihomo 实例记录写入失败：{error}") from error

    def _remove_instance_record(self, root: Path) -> None:
        path = self._record_path(root)
        for candidate in (path, path.with_suffix(path.suffix + ".bak")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    def _owned_persisted_process(self, record: dict[str, Any]) -> psutil.Process | None:
        try:
            pid = int(record.get("pid") or 0)
            if pid <= 0:
                return None
            process = psutil.Process(pid)
            if abs(float(process.create_time()) - float(record.get("create_time") or 0)) > 2:
                return None
            if not self._same_path(process.exe(), str(record.get("executable") or "")):
                return None
            root = str(record.get("root") or "")
            config = str(record.get("config") or "")
            command_paths = [item for item in process.cmdline() if item and not str(item).startswith("-")]
            if not any(self._same_path(item, root) for item in command_paths):
                return None
            if not any(self._same_path(item, config) for item in command_paths):
                return None
            return process
        except (psutil.Error, OSError, TypeError, ValueError):
            return None

    def _stop_persisted_instances_locked(self, *, exclude_pids: set[int] | None = None) -> None:
        excluded = exclude_pids or set()
        if not RUNTIME_ROOT.exists():
            return
        for path in RUNTIME_ROOT.glob("thread-*/managed-instance.json"):
            try:
                record = read_json(path, {}, expected_schema_version=1)
            except JsonStoreReadError:
                continue
            if not isinstance(record, dict):
                continue
            try:
                pid = int(record.get("pid") or 0)
            except (TypeError, ValueError):
                self._remove_instance_record(path.parent)
                continue
            if pid in excluded:
                continue
            process = self._owned_persisted_process(record)
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                except psutil.Error:
                    pass
            self._remove_instance_record(path.parent)
        self._stop_legacy_orphans_locked(excluded)

    def _stop_legacy_orphans_locked(self, excluded: set[int]) -> None:
        runtime_roots = [path.resolve() for path in RUNTIME_ROOT.glob("thread-*") if path.is_dir()]
        if not runtime_roots:
            return
        for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                pid = int(process.info.get("pid") or 0)
                if pid in excluded:
                    continue
                executable = str(process.info.get("exe") or "")
                name = str(process.info.get("name") or "")
                if "mihomo" not in f"{Path(executable).name} {name}".casefold():
                    continue
                command_paths = [item for item in (process.info.get("cmdline") or []) if item and not str(item).startswith("-")]
                owned = any(
                    any(self._same_path(item, root) for item in command_paths)
                    and any(self._same_path(item, root / "config.yaml") for item in command_paths)
                    for root in runtime_roots
                )
                if not owned:
                    continue
                process.terminate()
                try:
                    process.wait(timeout=3)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            except (psutil.Error, OSError, TypeError, ValueError):
                continue


managed_mihomo_manager = ManagedMihomoManager()
atexit.register(managed_mihomo_manager.stop_owned)
