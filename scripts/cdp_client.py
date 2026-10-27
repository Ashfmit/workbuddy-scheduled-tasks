# -*- coding: utf-8 -*-
"""cdp_client.py — 最小依赖的 Chrome DevTools Protocol 客户端。

只提供 growth_auto.py 需要的两个能力，不引第三方库（标准库实现 WebSocket）：

  - list_targets(port)  列出本地 CDP 端口上可注入的页面 target
  - WS                 一个只会「发文本帧 / 收一帧 / 关闭」的极简 WebSocket

为什么需要它：WorkBuddy 是 Electron 客户端，开调试端口后可以用 CDP 往页面里
注入 JS，复用页面自身已登录的会话来调接口——不需要读取任何令牌。

注意：urllib 默认尊重环境里的 HTTP_PROXY，连 127.0.0.1 都会被塞进代理，
导致「端口明明开着却报连不上」。list_targets 里强制直连（ProxyHandler({})）。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.error
import urllib.request
from typing import Any


class WS:
    """只做一件事：发一条文本帧。够用就行，不引第三方库。"""

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        if url.startswith("ws://"):
            rest = url[len("ws://"):]
        else:
            raise ValueError(f"只支持 ws:// 地址：{url}")
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.path = path
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手期间连接被关闭")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise ConnectionError(f"握手失败：{head.splitlines()[0] if head else '空响应'}")

    def send_text(self, payload: str) -> None:
        if self.sock is None:
            raise RuntimeError("还没连接")
        data = payload.encode("utf-8")
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def recv_frame(self) -> bytes | None:
        """读一帧，返回解码后的 payload；连接关闭返回 None。"""
        if self.sock is None:
            return None
        try:
            head = self._recv_exact(2)
            if not head:
                return None
            b1, b2 = head[0], head[1]
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x8:  # close
                return None
            return payload
        except (socket.timeout, OSError):
            return None

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("连接中断")
            out += chunk
        return out

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def list_targets(port: int) -> list[dict[str, Any]] | None:
    """列出可注入的页面 target。返回 None 表示端口没开。"""
    url = f"http://127.0.0.1:{port}/json/list"
    # 关键：环境里有全局 HTTP_PROXY 时，urllib 默认会把 127.0.0.1 也塞进代理，
    # 导致「端口明明开着却报连不上」。这里强制直连。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError:
        return None

    pages = []
    for item in data:
        if item.get("type") != "page":
            continue
        ws_url = item.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        if str(item.get("url", "")).startswith("devtools://"):
            continue
        pages.append(item)
    return pages
