"""MiniAgent-Code 配置加载。

优先级：CLI 覆盖参数 > 环境变量 AGENT_* > agent_config.json > 常见厂商环境变量自动探测 > 报错提示。
纯标准库实现，Python >= 3.8 可用。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = PROJECT_ROOT / "agent_config.json"

# 常见 OpenAI 兼容厂商探测：(环境变量名, 默认 base_url, 默认模型)
PROVIDER_PROBES = [
    ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    ("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
]


class Config:
    """运行时配置。"""

    def __init__(self, data: Dict[str, Any]):
        d = dict(data)
        self.api_base: str = (d.get("api_base") or "").rstrip("/")
        self.api_key: str = d.get("api_key") or ""
        self.model: str = d.get("model") or ""
        self.temperature: float = float(d.get("temperature", 0.7))
        self.max_tokens: int = int(d.get("max_tokens", 4096))
        self.max_steps: int = int(d.get("max_steps", 40))
        self.request_timeout_sec: int = int(d.get("request_timeout_sec", 180))
        self.context_limit_chars: int = int(d.get("context_limit_chars", 48000))
        self.memory_top_k: int = int(d.get("memory_top_k", 6))
        self.shell: str = d.get("shell") or "auto"  # auto / powershell / cmd / bash
        self.command_timeout_sec: int = int(d.get("command_timeout_sec", 120))
        self.confirm_dangerous: bool = bool(d.get("confirm_dangerous", True))
        self.auto_learn: bool = bool(d.get("auto_learn", True))
        self.use_mcp: bool = bool(d.get("use_mcp", True))
        self.mcp_config_path: Path = PROJECT_ROOT / (d.get("mcp_config") or "mcp_servers.json")
        self.skills_dirs: List[Path] = [
            Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p
            for p in (d.get("skills_dirs") or ["skills"])
        ]
        self.workspace: Path = PROJECT_ROOT

    @property
    def ok(self) -> bool:
        return bool(self.api_key and self.api_base and self.model)

    def describe(self) -> str:
        key = self.api_key
        masked = (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("已设置" if key else "未设置")
        return (
            "  api_base        = {}\n"
            "  api_key         = {}\n"
            "  model           = {}\n"
            "  max_steps       = {}\n"
            "  shell           = {}\n"
            "  auto_learn      = {}\n"
            "  mcp 配置        = {}\n"
            "  skills 目录     = {}".format(
                self.api_base or "(空)",
                masked,
                self.model or "(空)",
                self.max_steps,
                self.shell,
                self.auto_learn,
                self.mcp_config_path,
                ", ".join(str(p) for p in self.skills_dirs),
            )
        )


def load_config(overrides: Optional[Dict[str, Any]] = None) -> Config:
    """加载配置。overrides 里的键优先级最高（来自 CLI）。"""
    data: Dict[str, Any] = {}

    # 1) 配置文件 agent_config.json
    if CONFIG_FILE.exists():
        try:
            data.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            raise ValueError("解析 agent_config.json 失败：{}".format(e))

    # 2) 环境变量 AGENT_API_BASE / AGENT_API_KEY / AGENT_MODEL
    env_map = {
        "api_base": "AGENT_API_BASE",
        "api_key": "AGENT_API_KEY",
        "model": "AGENT_MODEL",
    }
    for key, env in env_map.items():
        val = os.environ.get(env, "").strip()
        if val:
            data[key] = val
    for env_key, cast in (("AGENT_MAX_STEPS", int), ("AGENT_SHELL", str)):
        val = os.environ.get(env_key, "").strip()
        if val:
            data[env_key.split("AGENT_")[1].lower()] = cast(val)

    # 3) 常见厂商环境变量自动探测
    if not data.get("api_key"):
        for env_name, base, model in PROVIDER_PROBES:
            if os.environ.get(env_name, "").strip():
                data.setdefault("api_key", os.environ[env_name].strip())
                data.setdefault("api_base", base)
                data.setdefault("model", model)
                break

    # 4) CLI 覆盖
    if overrides:
        for key, val in overrides.items():
            if val is not None:
                data[key] = val

    cfg = Config(data)
    return cfg


def config_help() -> str:
    return (
        "未找到可用的 LLM API 配置。请任选一种方式配置：\n"
        "  方式一：设置环境变量 AGENT_API_KEY / AGENT_API_BASE / AGENT_MODEL\n"
        "          （也支持自动探测 DEEPSEEK_API_KEY、OPENAI_API_KEY 等）\n"
        "  方式二：复制 agent_config.example.json 为 agent_config.json，填入 api_key\n"
        "  方式三：离线体验：python run_agent.py --mock \"写一个网页\"（无需 API）"
    )
