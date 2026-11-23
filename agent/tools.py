"""工具注册表 + 内置工具：Shell 执行、文件读写、记忆工具、Skill 工具。

所有工具统一为：name / description / JSON Schema 参数 / handler(dict)->str。
dispatch() 负责参数解析、异常兜底、输出截断，保证 agent 循环不会因单工具崩溃。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_TOOL_OUTPUT = 12000

# 仅对真正危险的命令要求人工确认（保持低误伤）
DANGEROUS_PATTERNS = [
    r"\bformat\b.*:", r"\bdiskpart\b", r"\bshutdown\b", r"\breboot\b",
    r"\bmkfs\b", r"rm\s+-rf\s+/\s", r"Remove-Item\s+.*-Recurse.*-\w*[A-Za-z]:\\",
    r"\breg\s+delete\s+HKLM", r"\bdisk wipe\b", r"\bdd\s+if=.*of=/dev/",
]


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-(limit // 2):]
    return head + "\n...[输出过长，已截断 {} 字符]...\n".format(len(text) - limit) + tail


class Tool:
    def __init__(self, name: str, description: str, parameters: Dict[str, Any],
                 handler: Callable[[Dict[str, Any]], str]):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON Schema
        self.handler = handler

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: Dict[str, Tool] = {}
        self.event_hook: Optional[Callable[[str, str, str], None]] = None  # (name, args, result 预览)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError("工具重名: " + tool.name)
        self.tools[tool.name] = tool

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        return [t.to_openai() for t in self.tools.values()]

    def dispatch(self, name: str, args_json: str) -> str:
        tool = self.tools.get(name)
        if not tool:
            return "错误：未知工具 '{}'。可用工具: {}".format(name, ", ".join(self.tools))
        try:
            args = json.loads(args_json) if (args_json or "").strip() else {}
            if not isinstance(args, dict):
                args = {"value": args}
        except json.JSONDecodeError as e:
            hint = ""
            if "Unterminated" in str(e) or "Expecting" in str(e):
                hint = ("（参数疑似被 max_tokens 截断：请精简内容后重试，"
                        "或提示用户在 agent_config.json 调大 max_tokens）")
            return "错误：工具 '{}' 参数不是合法 JSON（{}）。{}请重新调用并传正确的 JSON。".format(name, e, hint)
        try:
            result = tool.handler(args)
        except Exception as e:  # noqa: BLE001
            result = "工具 '{}' 执行出错: {}: {}".format(name, type(e).__name__, e)
        result = str(result)
        if self.event_hook:
            try:
                self.event_hook(name, args_json, result[:300])
            except Exception:  # noqa: BLE001
                pass
        return truncate(result)

    def names(self) -> List[str]:
        return list(self.tools)


# ---------------------------------------------------------------------- #
# 内置工具
# ---------------------------------------------------------------------- #
def _decode_output(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _resolve_shell(preference: str) -> List[str]:
    """返回命令行模板列表（最后一位填用户命令）。"""
    if preference == "bash" or (preference == "auto" and os.name != "nt"):
        bash = shutil.which("bash")
        if bash:
            return [bash, "-lc"]
        sh = shutil.which("sh")
        if sh:
            return [sh, "-c"]
    if os.name == "nt":
        if preference in ("powershell", "auto"):
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if ps:
                return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
        if preference == "cmd" or preference == "auto":
            return ["cmd", "/c"]
        raise RuntimeError("未找到可用的 shell（{}）".format(preference))
    # 非 Windows 兜底
    return ["sh", "-c"]


def make_run_command_tool(cfg) -> Tool:
    """执行 shell 命令，返回 stdout/stderr/exit code。"""

    def handler(args: Dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("缺少 command 参数")
        timeout = int(args.get("timeout_sec") or cfg.command_timeout_sec)

        # 危险命令确认
        lowered = command.lower()
        for pat in DANGEROUS_PATTERNS:
            if re.search(pat, lowered, re.I):
                return (
                    "已拦截疑似危险命令（命中保护规则 {}）。\n"
                    "如确认需要执行，请让用户在交互模式中手动确认，"
                    "或修改命令避开危险操作。".format(pat)
                )

        argv = _resolve_shell(cfg.shell)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        start = time.time()
        try:
            proc = subprocess.run(
                argv + [command],
                cwd=str(cfg.workspace),
                capture_output=True,
                timeout=timeout,
                env=env,
            )
            out = _decode_output(proc.stdout or b"")
            err = _decode_output(proc.stderr or b"")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            return "命令超时（>{}s），已终止。可调大 timeout_sec 参数后重试。".format(timeout)
        except FileNotFoundError as e:
            return "无法启动 shell: {}（可在 agent_config.json 设置 shell 为 cmd/powershell/bash）".format(e)

        dur = time.time() - start
        parts = ["$ " + command, "exit_code: {}".format(code), "耗时: {:.1f}s".format(dur)]
        if out.strip():
            parts.append("stdout:\n" + out.strip())
        if err.strip():
            parts.append("stderr:\n" + err.strip())
        if not out.strip() and not err.strip():
            parts.append("（无输出）")
        return "\n".join(parts)

    return Tool(
        name="run_command",
        description=(
            "执行一条 shell 命令（Windows 默认 PowerShell，可用配置切换 cmd/bash），"
            "返回 stdout/stderr/exit code。适合：运行脚本、安装依赖、git 操作、系统操作、"
            "验证文件（如 python -m http.server 之外的快速检查）等。命令在项目工作目录中执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令字符串"},
                "timeout_sec": {"type": "integer", "description": "超时秒数，默认 120"},
            },
            "required": ["command"],
        },
        handler=handler,
    )


def _safe_workspace_path(path_str: str, workspace: Path, must_exist: bool = False) -> Path:
    """解析并校验路径必须位于工作区内。"""
    p = Path(path_str)
    if not p.is_absolute():
        p = workspace / p
    p = p.resolve()
    try:
        p.relative_to(workspace)
    except ValueError:
        raise ValueError("路径越出工作区: {}".format(p))
    if must_exist and not p.exists():
        raise FileNotFoundError("文件不存在: {}".format(p))
    return p


def make_file_tools(cfg) -> List[Tool]:
    """工作区内文件读写与目录浏览。"""

    def _safe(path_str: str, must_exist: bool) -> Path:
        return _safe_workspace_path(path_str, cfg.workspace, must_exist)

    def write_file(args: Dict[str, Any]) -> str:
        path = _safe(str(args.get("path") or ""), False)
        content = args.get("content")
        if content is None:
            raise ValueError("缺少 content 参数")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        return "已写入 {}（{} 字符）".format(path, len(str(content)))

    def read_file(args: Dict[str, Any]) -> str:
        path = _safe(str(args.get("path") or ""), True)
        if path.stat().st_size > 2_000_000:
            return "文件过大（>2MB），请用 run_command 分段查看。"
        text = path.read_text(encoding="utf-8", errors="replace")
        limit = int(args.get("max_chars") or 20000)
        return truncate(text, limit)

    def list_dir(args: Dict[str, Any]) -> str:
        path = _safe(str(args.get("path") or "."), True)
        if path.is_file():
            path = path.parent
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:200]
        lines = []
        for e in entries:
            if e.is_dir():
                lines.append("[目录] " + e.name + "/")
            else:
                lines.append("[文件] {} ({} B)".format(e.name, e.stat().st_size))
        return "\n".join(lines) or "（空目录）"

    return [
        Tool(
            name="write_file",
            description="把文本内容写入工作区内的文件（自动创建父目录），UTF-8 编码。适合写网页/代码/配置等。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的目标路径，如 output/index.html"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
        Tool(
            name="read_file",
            description="读取工作区内的文本文件内容（可截断）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "最多返回字符数，默认 20000"},
                },
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="list_dir",
            description="列出工作区内某目录的文件与子目录。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "默认工作区根目录"}},
            },
            handler=list_dir,
        ),
    ]


def make_memory_tools(memory, cfg) -> List[Tool]:
    def save(args: Dict[str, Any]) -> str:
        entry = memory.add_memory(
            content=str(args.get("content") or ""),
            mtype=str(args.get("type") or "fact"),
            tags=[str(t) for t in (args.get("tags") or [])] if isinstance(args.get("tags"), list) else [],
        )
        return "已保存长期记忆 #{}：{}".format(entry["id"], entry["content"][:80])

    def search(args: Dict[str, Any]) -> str:
        hits = memory.search(str(args.get("query") or ""), int(args.get("k") or cfg.memory_top_k))
        if not hits:
            return "没有匹配的长期记忆。"
        return "\n".join(
            "#{} [{}] {}（{}）".format(e["id"], e["type"], e["content"], e["time"][:10]) for e in hits
        )

    def list_mem(args: Dict[str, Any]) -> str:
        items = memory.recent(int(args.get("limit") or 10))
        if not items:
            return "长期记忆为空。"
        return "\n".join(
            "#{} [{}] {}（{}）".format(e["id"], e["type"], e["content"], e["time"][:10]) for e in items
        )

    return [
        Tool(
            name="memory_save",
            description=(
                "把值得长期记住的信息（用户偏好、重要事实、项目结论、经验教训）写入持久化长期记忆"
                "（memory/long_term.json），跨会话可检索。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容（一句话，信息密度要高）"},
                    "type": {"type": "string", "enum": list(("fact", "preference", "event", "skill", "code"))},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content"],
            },
            handler=save,
        ),
        Tool(
            name="memory_search",
            description="按关键词检索长期记忆。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "返回条数，默认 6"},
                },
                "required": ["query"],
            },
            handler=search,
        ),
        Tool(
            name="memory_list",
            description="列出最近的长期记忆。",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            handler=list_mem,
        ),
    ]


def make_skill_tool(skill_manager) -> Tool:
    def handler(args: Dict[str, Any]) -> str:
        name = str(args.get("skill_name") or "").strip()
        return skill_manager.load_into_prompt(name)

    return Tool(
        name="use_skill",
        description=(
            "加载一个 skill 的完整执行指令。当用户任务与某个 skill 的描述匹配时，"
            "必须先调用本工具加载对应 skill，再按指令执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "skill 名称，来自系统提示词中的 skill 目录"},
            },
            "required": ["skill_name"],
        },
        handler=handler,
    )


def make_edit_file_tool(cfg) -> Tool:
    """对已有文件做精确文本替换（编程助手小步修改代码的首选工具）。"""

    def handler(args: Dict[str, Any]) -> str:
        path = _safe_workspace_path(str(args.get("path") or ""), cfg.workspace, must_exist=True)
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        replace_all = bool(args.get("replace_all") or False)
        if not old:
            raise ValueError("缺少 old_string")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError("old_string 在文件中未找到（需精确匹配，含缩进与换行；可先 read_file 核对原文）")
        if count > 1 and not replace_all:
            raise ValueError("old_string 出现 {} 次，不唯一；请扩大上下文使其唯一，或传 replace_all=true".format(count))
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(new_text, encoding="utf-8")
        replaced = count if replace_all else 1
        return "已编辑 {}：替换 {} 处，文件现 {} 字符".format(path.name, replaced, len(new_text))

    return Tool(
        name="edit_file",
        description=(
            "对工作区内已有文件做精确查找替换（小步修改代码/配置的首选，避免整文件重写）。"
            "old_string 必须与文件内容完全一致（含缩进与换行）；多处匹配时扩大上下文或传 replace_all=true。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径（相对工作区）"},
                "old_string": {"type": "string", "description": "要被替换的精确原文"},
                "new_string": {"type": "string", "description": "替换后的新文本（删除内容时传空字符串）"},
                "replace_all": {"type": "boolean", "description": "替换全部匹配；默认仅第一处且要求唯一"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=handler,
    )
