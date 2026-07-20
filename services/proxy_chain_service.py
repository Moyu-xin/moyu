from __future__ import annotations

import base64
import hashlib
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from services.proxy_service import normalize_proxy_url


CONNECT_TIMEOUT = 30
IDLE_TIMEOUT = 90
MAX_HEADER_BYTES = 128 * 1024


@dataclass(frozen=True)
class ProxyEndpoint:
    url: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def _parse_proxy(url: str, *, default_scheme: str = "http") -> ProxyEndpoint:
    normalized = normalize_proxy_url(str(url or "").strip())
    if normalized and "://" not in normalized:
        normalized = f"{default_scheme}://{normalized}"
    parsed = urlsplit(normalized)
    scheme = (parsed.scheme or default_scheme).lower()
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 1080 if scheme.startswith("socks") else 80)
    if not host:
        raise ValueError("代理地址缺少 host")
    return ProxyEndpoint(
        url=normalized,
        scheme=scheme,
        host=host,
        port=int(port),
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def _basic_auth_header(endpoint: ProxyEndpoint) -> str:
    if not endpoint.username:
        return ""
    token = base64.b64encode(f"{endpoint.username}:{endpoint.password}".encode("utf-8", errors="ignore")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def _read_until(sock: socket.socket, marker: bytes = b"\r\n\r\n", max_bytes: int = MAX_HEADER_BYTES) -> tuple[bytes, bytes]:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > max_bytes:
            raise RuntimeError("header too large")
    if marker in data:
        head, rest = data.split(marker, 1)
        return head + marker, rest
    return data, b""


def _status_code_from_response(data: bytes) -> int:
    try:
        first = data.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = first.split()
        return int(parts[1]) if len(parts) >= 2 else 0
    except Exception:
        return 0


def _connect_direct(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT)


def _connect_via_http_proxy(parent: ProxyEndpoint, host: str, port: int) -> socket.socket:
    sock = _connect_direct(parent.host, parent.port)
    sock.settimeout(CONNECT_TIMEOUT)
    target = f"{host}:{int(port)}"
    request = (
        f"CONNECT {target} HTTP/1.1\r\n"
        f"Host: {target}\r\n"
        f"{_basic_auth_header(parent)}"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    ).encode("iso-8859-1", errors="ignore")
    sock.sendall(request)
    response, _rest = _read_until(sock)
    code = _status_code_from_response(response)
    if code < 200 or code >= 300:
        sock.close()
        raise RuntimeError(f"Clash/父代理 CONNECT {target} 失败：HTTP {code or '未知'}")
    return sock


def _socks5_read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("SOCKS5 父代理连接中断")
        data += chunk
    return data


def _connect_via_socks5(parent: ProxyEndpoint, host: str, port: int) -> socket.socket:
    sock = _connect_direct(parent.host, parent.port)
    sock.settimeout(CONNECT_TIMEOUT)
    if parent.username:
        sock.sendall(b"\x05\x02\x00\x02")
        method = _socks5_read_exact(sock, 2)
        if method != b"\x05\x02":
            sock.close()
            raise RuntimeError("SOCKS5 父代理不接受用户名密码认证")
        username = parent.username.encode("utf-8", errors="ignore")[:255]
        password = parent.password.encode("utf-8", errors="ignore")[:255]
        sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
        auth = _socks5_read_exact(sock, 2)
        if auth != b"\x01\x00":
            sock.close()
            raise RuntimeError("SOCKS5 父代理认证失败")
    else:
        sock.sendall(b"\x05\x01\x00")
        method = _socks5_read_exact(sock, 2)
        if method != b"\x05\x00":
            sock.close()
            raise RuntimeError("SOCKS5 父代理不接受免密认证")

    host_bytes = host.encode("idna")[:255]
    request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + int(port).to_bytes(2, "big")
    sock.sendall(request)
    response = _socks5_read_exact(sock, 4)
    if response[1] != 0:
        sock.close()
        raise RuntimeError(f"SOCKS5 父代理连接 {host}:{port} 失败：{response[1]}")
    atyp = response[3]
    if atyp == 1:
        _socks5_read_exact(sock, 4)
    elif atyp == 3:
        length = _socks5_read_exact(sock, 1)[0]
        _socks5_read_exact(sock, length)
    elif atyp == 4:
        _socks5_read_exact(sock, 16)
    _socks5_read_exact(sock, 2)
    return sock


def _connect_to_upstream_proxy(upstream: ProxyEndpoint, parent: ProxyEndpoint) -> socket.socket:
    if parent.scheme.startswith("socks"):
        return _connect_via_socks5(parent, upstream.host, upstream.port)
    return _connect_via_http_proxy(parent, upstream.host, upstream.port)


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    for sock in sockets:
        sock.settimeout(None)
    deadline = time.time() + IDLE_TIMEOUT
    while True:
        timeout = max(1.0, deadline - time.time())
        if timeout <= 0:
            break
        readable, _, errored = select.select(sockets, [], sockets, timeout)
        if errored:
            break
        if not readable:
            break
        for source in readable:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            deadline = time.time() + IDLE_TIMEOUT
            target = right if source is left else left
            try:
                target.sendall(data)
            except OSError:
                return


class _ChainServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], upstream: ProxyEndpoint, parent: ProxyEndpoint):
        self.upstream = upstream
        self.parent = parent
        super().__init__(server_address, _ChainHandler)


class _ChainHandler(socketserver.BaseRequestHandler):
    server: _ChainServer

    def handle(self) -> None:
        client = self.request
        client.settimeout(CONNECT_TIMEOUT)
        upstream_sock: socket.socket | None = None
        try:
            header, rest = _read_until(client)
            if not header:
                return
            try:
                header_text = header.decode("iso-8859-1", errors="replace")
                first_line = header_text.split("\r\n", 1)[0]
                method, target, version = first_line.split(" ", 2)
            except ValueError:
                self._send_error(400, "Bad request")
                return

            upstream_sock = _connect_to_upstream_proxy(self.server.upstream, self.server.parent)
            upstream_sock.settimeout(CONNECT_TIMEOUT)
            if method.upper() == "CONNECT":
                self._handle_connect(upstream_sock, target, rest)
            else:
                self._handle_http(upstream_sock, header_text, rest)
        except Exception as exc:
            try:
                self._send_error(502, f"Proxy chain failed: {exc}")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass
            if upstream_sock is not None:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    def _send_error(self, code: int, message: str) -> None:
        body = str(message or "").encode("utf-8", errors="ignore")[:800]
        response = (
            f"HTTP/1.1 {code} Proxy Chain Error\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("iso-8859-1") + body
        self.request.sendall(response)

    def _handle_connect(self, upstream_sock: socket.socket, target: str, rest: bytes) -> None:
        request = (
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"{_basic_auth_header(self.server.upstream)}"
            "Proxy-Connection: Keep-Alive\r\n"
            "\r\n"
        ).encode("iso-8859-1", errors="ignore")
        upstream_sock.sendall(request)
        response, _response_rest = _read_until(upstream_sock)
        code = _status_code_from_response(response)
        if code < 200 or code >= 300:
            raise RuntimeError(f"住宅代理 CONNECT {target} 失败：HTTP {code or '未知'}")
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: moyu-chain\r\n\r\n")
        if rest:
            upstream_sock.sendall(rest)
        _relay(self.request, upstream_sock)

    def _handle_http(self, upstream_sock: socket.socket, header_text: str, rest: bytes) -> None:
        lines = header_text.split("\r\n")
        first_line = lines[0]
        output = [first_line]
        has_host = False
        for line in lines[1:]:
            if not line:
                continue
            name = line.split(":", 1)[0].strip().lower()
            if name == "host":
                has_host = True
            if name in {"proxy-authorization", "proxy-connection"}:
                continue
            output.append(line)
        if not has_host:
            output.append(f"Host: {urlsplit(first_line.split(' ', 2)[1]).netloc}")
        output.append("Proxy-Connection: Keep-Alive")
        auth = _basic_auth_header(self.server.upstream).strip()
        if auth:
            output.append(auth)
        upstream_sock.sendall(("\r\n".join(output) + "\r\n\r\n").encode("iso-8859-1", errors="ignore") + rest)
        _relay(self.request, upstream_sock)


@dataclass
class _ChainEntry:
    key: str
    upstream_url: str
    parent_url: str
    server: _ChainServer
    thread: threading.Thread
    created_at: float
    last_used_at: float

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


class ProxyChainManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _ChainEntry] = {}

    def proxy_for(self, upstream_proxy: str, parent_proxy: str) -> str:
        upstream = _parse_proxy(upstream_proxy, default_scheme="http")
        parent = _parse_proxy(parent_proxy, default_scheme="http")
        key = hashlib.sha256(f"{upstream.url}\n{parent.url}".encode("utf-8", errors="ignore")).hexdigest()
        with self._lock:
            self._cleanup_locked()
            entry = self._entries.get(key)
            if entry:
                entry.last_used_at = time.time()
                return entry.url
            server = _ChainServer(("127.0.0.1", 0), upstream, parent)
            thread = threading.Thread(target=server.serve_forever, name=f"proxy-chain-{server.server_address[1]}", daemon=True)
            thread.start()
            entry = _ChainEntry(
                key=key,
                upstream_url=upstream.url,
                parent_url=parent.url,
                server=server,
                thread=thread,
                created_at=time.time(),
                last_used_at=time.time(),
            )
            self._entries[key] = entry
            return entry.url

    def _cleanup_locked(self) -> None:
        now = time.time()
        stale_keys = [key for key, entry in self._entries.items() if now - entry.last_used_at > 2 * 60 * 60]
        for key in stale_keys:
            entry = self._entries.pop(key, None)
            if not entry:
                continue
            try:
                entry.server.shutdown()
                entry.server.server_close()
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": len(self._entries),
                "items": [
                    {
                        "local_proxy": entry.url,
                        "upstream_proxy_hash": hashlib.sha256(entry.upstream_url.encode("utf-8", errors="ignore")).hexdigest()[:12],
                        "parent_proxy": entry.parent_url,
                        "last_used_at": entry.last_used_at,
                    }
                    for entry in self._entries.values()
                ],
            }


proxy_chain_manager = ProxyChainManager()
