"""Agent 主循环：系统提示词（记忆 + skill 目录）-> LLM -> 工具调用 -> 直至给出最终答案。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .config import Config
from .llm import LLMClient, LLMError
from .memory import Memory
from .skills import SkillManager
from .tools import ToolRegistry, make_run_command_tool, make_file_tools, make_memory_tools, make_skill_tool
from .mcp import MCPManager

MAX_RESULT_PREVIEW = 160


def _preview(text: str, n: int = MAX_RESULT_PREVIEW) -> str:
    text = " ".join(str(text).split())
    return text[:n] + ("…" if len(text) > n else "")


class Agent:
    def __init__(self, cfg: Config, memory: Memory, skills: SkillManager,
                 llm, registry: ToolRegistry, mcp: Optional[MCPManager] = None,
                 quiet: bool = False):
        self.cfg = cfg
        self.memory = memory
        self.skills = skills
        self.llm = llm
        self.registry = registry
        self.mcp = mcp
        self.quiet = quiet
        self.sess = memory.new_session()
        self.active_skills: List[str] = []
        # 工具事件回显
        self.registry.event_hook = self._on_tool_event

    # ------------------------------------------------------------------ #
    def _on_tool_event(self, name: str, args_json: str, result_preview: str) -> None:
        self._print("    [工具] {}({}) -> {}".format(name, _preview(args_json, 120), _preview(result_preview)))
        if name == "use_skill":
            try:
                args = json.loads(args_json)
                skill = str(args.get("skill_name") or "")
                if skill not in self.active_skills:
                    self.active_skills.append(skill)
            except Exception:  # noqa: BLE001
                pass

    def _print(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    # ------------------------------------------------------------------ #
    def build_system_prompt(self, query: str = "") -> str:
        memory_block = self.memory.context_block(query)
        skill_block = self.skills.catalog_block()
        parts = [
            "你是 MiniAgent-Code，一个务实的编程助手智能体：理解编程需求，动手写代码、调试、重构、"
            "运行验证，直到任务真正完成。",
            "核心原则：",
            "1) 工具优先，结论以工具输出为准，绝不臆造运行结果或 API 行为；"
            "写代码用 write_file/edit_file（改已有文件首选 edit_file），看代码先 read_file，"
            "执行与验证用 run_command（python/pytest/pip/git 等）；",
            "2) 编码规范：优先标准库与最小依赖；小步修改，改完即验证；报错先读完整输出再修，不盲改；",
            "3) 长期记忆持久化在 memory/ 目录：用 memory_save 记住用户技术栈、代码风格偏好、项目结构等，"
            "用 memory_search/memory_list 检索；",
            "4) 工作目录：{}（文件工具与命令均在此目录执行；新项目放 output/ 子目录，"
            "改现有文件前先 read_file 核对原文）".format(self.cfg.workspace),
            "5) 当前时间：{}".format(time.strftime("%Y-%m-%d %H:%M (%A)")),
            "",
            "## Skill 自主选择（重要）",
            "你可以使用以下 skill（每个 skill 是一套任务专属执行指令）：",
            skill_block or "（当前没有可用 skill）",
            "规则：拿到任务后，先判断是否命中某个 skill 的描述；命中就必须先调用 use_skill 加载它，"
            "再严格按其指令完成任务；一个任务可以先后加载多个 skill（如先 code-review 再 code-project）。"
            "没有匹配的 skill 时直接用基础工具完成。",
            "",
        ]
        if memory_block:
            parts += ["## 记忆上下文", memory_block, ""]
        parts += [
            "## 编程工作流",
            "- 需求有歧义：按最常见的技术方案假设进行，并在答复开头说明假设；",
            "- 复杂任务：先列出文件/模块清单与接口设计，再逐个实现，每步用工具验证；",
            "- 修复循环：运行 → 读报错 → 最小修复 → 再运行，直到通过；不要用 try/except 吞异常当作修复；",
            "- 完成后：用中文总结（改了什么、如何运行、验证结果、已知限制），新依赖给出安装命令。",
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    def run_turn(self, user_input: str) -> str:
        """执行一轮对话：必要时多次工具调用，返回最终答复文本。"""
        # 回合开始前做上下文压缩
        try:
            if self.memory.maybe_compress(self.sess, self.llm, self.cfg.context_limit_chars):
                self._print("  [记忆] 对话过长，已自动压缩较早内容并归档到 memory/history/")
        except Exception as e:  # noqa: BLE001
            self._print("  [记忆] 压缩失败（忽略）：{}".format(e))

        self.sess["messages"].append({"role": "user", "content": user_input})
        # system 提示词（身份+skill 目录+记忆）每次动态构建，不落库
        messages = [{"role": "system", "content": self.build_system_prompt(user_input)}] + \
            list(self.sess["messages"])
        tools = self.registry.to_openai_tools()
        final = ""

        for step in range(1, self.cfg.max_steps + 1):
            try:
                assistant, usage = self.llm.chat(messages, tools=tools)
            except LLMError as e:
                final = "LLM 调用失败：{}\n请检查 agent_config.json / 环境变量中的 API 配置。".format(e)
                self._print("  [LLM] " + final)
                break

            messages.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            if usage:
                self._print("  [LLM] 第 {} 步 tokens: {}".format(step, json.dumps(usage, ensure_ascii=False)))
            if not tool_calls:
                final = str(assistant.get("content") or "").strip() or "（模型返回空内容）"
                break

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "?")
                args_json = str(fn.get("arguments") or "{}")
                self._print("  [步骤 {}] 调用 {}: {}".format(step, name, _preview(args_json, 150)))
                result = self.registry.dispatch(name, args_json)
                self._print("    [结果] " + _preview(result))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "name": name,
                    "content": result,
                })
        else:
            final = "已达到最大步数（{}）。任务可能未完成，可继续对话让 agent 接着做。".format(self.cfg.max_steps)

        # 收尾：去掉头部 system 后存回会话
        stored = messages[1:] if (messages and messages[0].get("role") == "system") else messages
        self.sess["messages"] = stored
        self.memory.save_session(self.sess)
        return final

    # ------------------------------------------------------------------ #
    def shutdown(self, learn: bool = True) -> None:
        """会话结束：提取长期记忆 + 写会话摘要 + 关闭 MCP。"""
        if learn:
            try:
                res = self.memory.learn_from_session(self.sess, self.llm)
                if res["memories"]:
                    self._print("  [记忆] 已从会话提取 {} 条长期记忆".format(res["memories"]))
                if res["summary"]:
                    self._print("  [记忆] 会话摘要已写入 memory/summary.md")
            except Exception as e:  # noqa: BLE001
                self._print("  [记忆] 会话学习失败（忽略）：{}".format(e))
        self.memory.save_session(self.sess)
        if self.mcp:
            self.mcp.stop_all()
        self._print("  [记忆] 会话 {} 已保存（memory/sessions/{}.json）".format(self.sess["id"], self.sess["id"]))

    # ------------------------------------------------------------------ #
    def memory_stats(self) -> str:
        entries = self.memory.load_long_term()
        sessions = self.memory.list_sessions()
        return (
            "长期记忆 {} 条；历史会话 {} 个；当前会话 {}（{} 条消息，{} 次压缩，本回合 skill: {}）".format(
                len(entries), len(sessions), self.sess["id"],
                len(self.sess["messages"]), len(self.sess["summaries"]),
                ", ".join(self.active_skills) or "无",
            )
        )
