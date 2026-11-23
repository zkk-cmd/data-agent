"""MiniAgent-Code：具备长期记忆 / Skill / MCP / Shell 能力的编程助手 agent。"""
from .config import Config, load_config, config_help, PROJECT_ROOT
from .llm import LLMClient, MockLLM, LLMError
from .memory import Memory
from .skills import SkillManager
from .tools import Tool, ToolRegistry, make_run_command_tool, make_file_tools, make_edit_file_tool, make_memory_tools, make_skill_tool
from .mcp import MCPManager
from .core import Agent

__all__ = [
    "Config", "load_config", "config_help", "PROJECT_ROOT",
    "LLMClient", "MockLLM", "LLMError",
    "Memory", "SkillManager", "ToolRegistry",
    "make_run_command_tool", "make_file_tools", "make_edit_file_tool", "make_memory_tools", "make_skill_tool",
    "MCPManager", "Agent",
]

__version__ = "1.0.0"
