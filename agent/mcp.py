"""MCP (Model Context Protocol) 客户端：stdio 传输，JSON-RPC 2.0。

支持：
  - 启动/停止本地 MCP 服务器子进程（mcp_servers.json 配置）
  - initialize 握手 + tools/list 发现工具
  - tools/call 调用工具
  - ping / 通知处理、stderr 日志缓冲
MCP 工具将以 `mcp__<server>__<tool>` 的命名并入 agent 工具表。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT = 60
START_TIMEOUT = 30


class MCPError(Exception):
    pass


def _resolve_command(cmd: str) -> str:
    """Windows 下 npx/uvx 等是 .cmd/.bat，需要解析出真实可执行路径。"""
    p = shutil.which(cmd)
    if p:
        return p
    if os.name == "nt":
        for ext in (".cmd", ".bat", ".exe"):
            p = shutil.which(cmd + ext)
            if p:
                return p
    return cmd


class MCPServerConnection:
    def __init__(self, name: str, spec: Dict[str, Any], log_dir: Path):
        self.name = name
        self.spec = spec
        self.log_path = log_dir / ("mcp-{}.log".format(name))
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._events: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stderr_buf: deque = deque(maxlen=50)
        self.tools: List[Dict[str, Any]] = []
        self.alive = False

    # ---------------- 进程与读写线程 ---------------- #
    def start(self) -> None:
        command = _resolve_command(str(self.spec.get("command") or ""))
        args = [str(a) for a in (self.spec.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self.proc = subprocess.Popen(
                [command] + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.spec.get("cwd") or Path.cwd()),
            )
        except Exception as e:  # noqa: BLE001
            raise MCPError("启动 MCP 服务器 '{}' 失败: {} ({})".format(self.name, command, e))

        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr_pump, daemon=True).start()

        # initialize 握手
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mini-agent", "version": "1.0.0"},
        }, timeout=START_TIMEOUT)
        self.notify("notifications/initialized")
        server_info = result.get("serverInfo", {})
        tools_result = self.request("tools/list", {}, timeout=START_TIMEOUT)
        self.tools = tools_result.get("tools") or []
        self.alive = True
        self._log("started: {} v{} ({} tools)".format(
            server_info.get("name", "?"), server_info.get("version", "?"), len(self.tools)))

    def _log(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
        except OSError:
            pass

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._log("non-json stdout: " + line[:200])
                continue
            rid = msg.get("id")
            if isinstance(rid, int) and rid in self._events:
                with self._lock:
                    self._pending[rid] = msg
                    self._events[rid].set()
            elif msg.get("method") == "ping":
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                self._log("notify: " + json.dumps(msg, ensure_ascii=False)[:300])
        # 进程退出
        self.alive = False
        for ev in list(self._events.values()):
            ev.set()

    def _stderr_pump(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr_buf.append(line)
                self._log("stderr: " + line[:300])

    # ---------------- JSON-RPC ---------------- #
    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, method: str, params: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        if not self.proc:
            raise MCPError("服务器 {} 未启动".format(self.name))
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._events[rid] = threading.Event()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not self._events[rid].wait(timeout):
            with self._lock:
                self._events.pop(rid, None)
                self._pending.pop(rid, None)
            raise MCPError("请求 '{}' 超时（{}s）。stderr 末尾: {}".format(
                method, timeout, " | ".join(list(self._stderr_buf)[-3:])))
        with self._lock:
            msg = self._pending.pop(rid, None)
            self._events.pop(rid, None)
        if msg is None:
            raise MCPError("服务器 '{}' 已退出，无法完成请求 '{}'".format(self.name, method))
        if "error" in msg:
            err = msg["error"]
            raise MCPError("MCP 错误（{}）: {} {}".format(self.name, err.get("code"), err.get("message")))
        return msg.get("result") or {}

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------------- 工具 ---------------- #
    def list_tools(self) -> List[Dict[str, Any]]:
        return self.tools

    def call(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 120) -> str:
        result = self.request("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
        parts: List[str] = []
        for item in result.get("content") or []:
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text", "")))
            elif t == "resource":
                res = item.get("resource", {})
                parts.append("[resource] " + str(res.get("uri", "")) + "\n" + str(res.get("text", ""))[:2000])
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
        if result.get("isError"):
            text = "工具返回错误：\n" + text
        return text

    def stop(self) -> None:
        self.alive = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None


class MCPManager:
    def __init__(self, config_path: Path, log_dir: Path):
        self.config_path = config_path
        self.log_dir = log_dir
        self.conns: Dict[str, MCPServerConnection] = {}

    def load_specs(self) -> Dict[str, Dict[str, Any]]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print("[MCP] 配置解析失败 {}: {}".format(self.config_path, e))
            return {}
        servers = data.get("mcpServers") or {}
        return servers if isinstance(servers, dict) else {}

    def start_all(self) -> Dict[str, str]:
        """启动全部服务器，返回 {name: 状态描述}。失败不影响主流程。"""
        status: Dict[str, str] = {}
        for name, spec in self.load_specs().items():
            conn = MCPServerConnection(name, spec, self.log_dir)
            try:
                conn.start()
                self.conns[name] = conn
                status[name] = "已连接，{} 个工具".format(len(conn.tools))
            except Exception as e:  # noqa: BLE001
                status[name] = "连接失败: {}".format(e)
                self._print_fail(name, str(e))
        return status

    def _print_fail(self, name: str, err: str) -> None:
        print("[MCP] 服务器 '{}' 启动失败（已跳过，不影响其它功能）：{}".format(name, err))

    def all_tools(self) -> List[Dict[str, Any]]:
        """汇总各服务器工具 -> OpenAI function 定义。"""
        out: List[Dict[str, Any]] = []
        for sname, conn in self.conns.items():
            for t in conn.list_tools():
                tname = t.get("name") or ""
                out.append({
                    "type": "function",
                    "function": {
                        "name": "mcp__{}__{}".format(sname, tname),
                        "description": "[MCP:{}] {}".format(sname, t.get("description") or tname),
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                })
        return out

    def call(self, full_name: str, arguments: Dict[str, Any]) -> str:
        try:
            sname, tname = full_name.split("__", 1)[1].split("__", 1)
        except (IndexError, ValueError):
            return "错误的 MCP 工具名: {}".format(full_name)
        conn = self.conns.get(sname)
        if not conn or not conn.alive:
            return "MCP 服务器 '{}' 不可用".format(sname)
        try:
            return conn.call(tname, arguments)
        except MCPError as e:
            return "MCP 调用失败: {}".format(e)

    def stop_all(self) -> None:
        for conn in self.conns.values():
            conn.stop()
        self.conns.clear()
