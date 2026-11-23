#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniAgent-Code（编程助手）命令行入口。

用法：
  python run_agent.py                          交互模式（多轮对话）
  python run_agent.py "帮我写一个网页"          单任务模式
  python run_agent.py "调试修复 utils.py 的报错"
  python run_agent.py --mock "帮我写一个网页"   离线 Mock 演示（无需 API Key）
  python run_agent.py --session <id>           继续历史会话
  python run_agent.py --check                  自检（配置/记忆/skill/MCP，无需 API Key）
  python run_agent.py --list-tools             查看全部工具（含 MCP 工具，无需 API Key）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent import (  # noqa: E402
    Config, load_config, config_help, PROJECT_ROOT,
    LLMClient, MockLLM, LLMError, Memory, SkillManager,
    ToolRegistry, Tool, make_run_command_tool, make_file_tools, make_edit_file_tool, make_memory_tools, make_skill_tool,
    MCPManager, Agent, __version__,
)

BANNER = r"""
=============================================================
  MiniAgent-Code  v{}   编程助手 · 长期记忆 · Skill · MCP · Shell
=============================================================
  工作目录 : {}
  交互命令 : /exit 退出   /mem 记忆状态   /skills skill列表
             /tools 工具列表   /compact 立即压缩上下文
=============================================================
"""


def build_registry(cfg: Config, memory: Memory, skills: SkillManager) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_run_command_tool(cfg))
    for t in make_file_tools(cfg):
        registry.register(t)
    registry.register(make_edit_file_tool(cfg))
    for t in make_memory_tools(memory, cfg):
        registry.register(t)
    registry.register(make_skill_tool(skills))
    return registry


def try_start_mcp(cfg: Config) -> MCPManager | None:
    mcp = MCPManager(cfg.mcp_config_path, PROJECT_ROOT / "memory" / "logs")
    if not cfg.use_mcp:
        return None
    specs = mcp.load_specs()
    if not specs:
        return None
    print("[MCP] 启动 {} 个服务器…".format(len(specs)))
    status = mcp.start_all()
    for name, s in status.items():
        print("[MCP]   {} : {}".format(name, s))
    return mcp


def make_agent(cfg: Config, mock: bool = False, session_id: str | None = None,
               quiet_mcp_fail: bool = False) -> Agent:
    memory = Memory(PROJECT_ROOT / "memory", top_k=cfg.memory_top_k)
    skills = SkillManager(cfg.skills_dirs)
    registry = build_registry(cfg, memory, skills)
    llm = MockLLM(cfg) if mock else LLMClient(cfg)
    mcp = None if mock else try_start_mcp(cfg)
    if mcp:
        for t in mcp.all_tools():
            registry.register(
                Tool(
                    name=t["function"]["name"],
                    description=t["function"]["description"],
                    parameters=t["function"]["parameters"],
                    handler=(lambda mn: lambda args: mcp.call(mn, args))(t["function"]["name"]),
                )
            )
    agent = Agent(cfg, memory, skills, llm, registry, mcp=mcp)
    if session_id:
        sess = memory.load_session(session_id)
        if sess:
            agent.sess = sess
            print("[会话] 已恢复历史会话 {}".format(session_id))
        else:
            print("[会话] 未找到会话 {}，新建会话 {}".format(session_id, agent.sess["id"]))
    return agent


# ---------------------------------------------------------------------- #
def cmd_check(cfg: Config) -> int:
    """自检：不调用真实 LLM。"""
    print("== MiniAgent-Code 自检 ==")
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            msg = fn()
            print("  [PASS] {} {}".format(name, msg or ""))
        except Exception as e:  # noqa: BLE001
            ok = False
            print("  [FAIL] {} : {}: {}".format(name, type(e).__name__, e))

    memory = Memory(PROJECT_ROOT / "memory")
    step("记忆目录", lambda: str(memory.root))
    entry = memory.add_memory("自检测试条目（可删除）", "fact", ["selftest"], source="selftest")
    step("记忆写入", lambda: "#" + entry["id"])
    step("记忆检索", lambda: "{} 条命中".format(len(memory.search("自检", 3))))
    # 清理自检条目
    entries = [e for e in memory.load_long_term() if e["id"] != entry["id"]]
    (memory.root / "long_term.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    skills = SkillManager(cfg.skills_dirs)
    step("Skill 发现", lambda: "{} 个: {}".format(len(skills.skills), ", ".join(skills.skills)))
    if skills.skills:
        first = next(iter(skills.skills))
        step("Skill 加载", lambda: "加载 '{}' 共 {} 字符".format(first, len(skills.load_into_prompt(first))))

    registry = build_registry(cfg, memory, skills)
    step("内置工具", lambda: ", ".join(registry.names()))
    step("Shell 工具", lambda: registry.dispatch("run_command", json.dumps({"command": "echo miniagent-shell-ok"}))[:120])

    mcp = MCPManager(cfg.mcp_config_path, PROJECT_ROOT / "memory" / "logs")
    specs = mcp.load_specs()
    if specs:
        status = mcp.start_all()
        for name, s in status.items():
            print("  [MCP]   {} : {}".format(name, s))
        tools = mcp.all_tools()
        step("MCP 工具发现", lambda: "{} 个 MCP 工具".format(len(tools)))
        demo = [t["function"]["name"] for t in tools if "now" in t["function"]["name"]]
        if demo:
            step("MCP 工具调用", lambda: registry.dispatch(demo[0], "{}")[:120])
        mcp.stop_all()
    else:
        print("  [SKIP] MCP：mcp_servers.json 未配置服务器")

    if not cfg.ok:
        print("\n  [WARN] 尚未配置 LLM API（Mock 模式可用：--mock）")
        print("  " + config_help().replace("\n", "\n  "))
    else:
        print("  [PASS] LLM 配置: {} / {}".format(cfg.api_base, cfg.model))
    print("== 自检{} ==".format("通过" if ok else "未全部通过"))
    return 0 if ok else 1


def cmd_task(agent: Agent, task: str, learn: bool) -> int:
    print("[任务] " + task)
    print("-" * 60)
    answer = agent.run_turn(task)
    print("-" * 60)
    print(answer)
    agent.shutdown(learn=learn)
    return 0


def cmd_repl(agent: Agent, learn: bool) -> int:
    print(BANNER.format(__version__, agent.cfg.workspace))
    print("会话 {} 已开始，输入任务开始（Ctrl+C / /exit 退出）。\n".format(agent.sess["id"]))
    while True:
        try:
            line = input("\n你> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            break
        if line == "/mem":
            print(agent.memory_stats())
            continue
        if line == "/skills":
            print(agent.skills.stats())
            continue
        if line == "/tools":
            print("\n".join(agent.registry.names()))
            continue
        if line == "/compact":
            done = agent.memory.maybe_compress(agent.sess, agent.llm, agent.cfg.context_limit_chars // 2)
            print("压缩" + ("完成" if done else "未触发（长度不足）"))
            continue
        print("-" * 60)
        answer = agent.run_turn(line)
        print("-" * 60)
        print(answer)
    agent.shutdown(learn=learn)
    print("再见。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniAgent-Code - 编程助手（长期记忆/Skill/MCP/Shell）")
    parser.add_argument("task", nargs="*", help="单任务模式：直接给出任务文本；留空进入交互模式")
    parser.add_argument("--session", help="继续指定 id 的历史会话")
    parser.add_argument("--mock", action="store_true", help="离线 Mock 模式（无需 API Key，剧本化演示）")
    parser.add_argument("--check", action="store_true", help="自检记忆/Skill/Shell/MCP（不调用 LLM）")
    parser.add_argument("--list-tools", action="store_true", help="列出全部工具后退出")
    parser.add_argument("--no-learn", action="store_true", help="会话结束时不做记忆提取/摘要")
    parser.add_argument("--no-mcp", action="store_true", help="禁用 MCP")
    parser.add_argument("--max-steps", type=int, help="单轮最大工具调用步数")
    parser.add_argument("--yes", action="store_true", help="跳过危险命令确认（当前版本危险命令一律拦截）")
    args = parser.parse_args()

    overrides = {}
    if args.max_steps:
        overrides["max_steps"] = args.max_steps
    if args.no_mcp:
        overrides["use_mcp"] = False

    cfg = load_config(overrides)

    if args.check:
        return cmd_check(cfg)

    memory = Memory(PROJECT_ROOT / "memory")
    skills = SkillManager(cfg.skills_dirs)
    registry = build_registry(cfg, memory, skills)

    if args.list_tools:
        if cfg.use_mcp:
            mcp = MCPManager(cfg.mcp_config_path, PROJECT_ROOT / "memory" / "logs")
            specs = mcp.load_specs()
            if specs:
                mcp.start_all()
                for t in mcp.all_tools():
                    registry.register(
                        Tool(
                            name=t["function"]["name"], description=t["function"]["description"],
                            parameters=t["function"]["parameters"],
                            handler=(lambda mn: lambda a: mcp.call(mn, a))(t["function"]["name"]),
                        )
                    )
                mcp.stop_all()
        for name in registry.names():
            t = registry.tools[name]
            print("{}\n    {}".format(name, " ".join(t.description.split())))
        return 0

    if not cfg.ok and not args.mock:
        print(config_help())
        return 2

    agent = make_agent(cfg, mock=args.mock, session_id=args.session)
    task_text = " ".join(args.task).strip()
    if task_text:
        return cmd_task(agent, task_text, learn=cfg.auto_learn and not args.no_learn)
    return cmd_repl(agent, learn=cfg.auto_learn and not args.no_learn)


if __name__ == "__main__":
    sys.exit(main())
