# data-agent · MiniAgent-Code 编程助手

一个**零第三方依赖**（纯 Python 标准库，≥3.8）的命令行**编程助手**：接任意
OpenAI 兼容大模型 API（已配 GLM-5.3-Flash），即可自主写代码、调试、重构、
运行验证，把有价值的编程上下文沉淀为跨会话的长期记忆。

> 仓库：https://github.com/zkk-cmd/data-agent
> 前身是通用智能体 MiniAgent，现已定位为编程助手：身份/工作流/技能组全部面向编程任务。

## 需求实现对照

| # | 能力 | 实现 | 代码 |
|---|------|------|------|
| 1 | 长记忆持久化 + 摘要 | `memory/` 三层持久化（记忆条目 JSON / 会话状态 / 原始归档），三层摘要（条目浓缩、上下文压缩、会话总结）；记忆类型含 `code`（技术栈/项目约定） | `agent/memory.py` |
| 2 | MCP 接入 | stdio JSON-RPC 2.0 客户端，`mcp_servers.json` 配置，工具以 `mcp__<server>__<tool>` 并入 | `agent/mcp.py` |
| 3 | Shell 执行 | `run_command`（PowerShell/cmd/bash 自适应、超时、危险命令拦截） | `agent/tools.py` |
| 4 | Skill 接入 | `skills/*/SKILL.md` 声明式 skill，6 个编程向技能 | `agent/skills.py` + `skills/` |
| 5 | 自主选择 skill | skill 目录随系统提示词下发，模型自主调用 `use_skill` | `agent/core.py` |
| + | 代码编辑 | `write_file` / `read_file` / `list_dir` / `edit_file`（精确查找替换，小步改码） | `agent/tools.py` |

## 编程助手的 Skill 组

| Skill | 何时使用 |
|-------|---------|
| `code-project` | 写脚本/程序、建项目结构、写测试 |
| `web-dev` | 网页/HTML/CSS/JS 前端开发 |
| `debug-expert` | 报错/异常/bug 定位与修复（复现→定位→最小修复→回归验证） |
| `code-review` | 代码审查、重构、性能优化建议 |
| `shell-helper` | 装依赖/虚拟环境、跑测试、git 操作、进程端口 |
| `writing` | README / API 文档 / 设计文档 / 注释 |

新增 skill 只需建目录写 `SKILL.md`（frontmatter 的 `description` 决定 agent 何时用它）。

## 目录结构

```
├── run_agent.py            # CLI 入口（交互 / 单任务 / 自检 / Mock）
├── agent/
│   ├── config.py           # 配置：agent_config.json + 环境变量 + 厂商自动探测
│   ├── llm.py              # OpenAI 兼容客户端（工具调用）+ 离线 MockLLM
│   ├── memory.py           # 长期记忆：持久化 / 摘要 / 检索 / 会话学习
│   ├── skills.py           # Skill 发现与加载
│   ├── tools.py            # 工具注册表 + shell/文件/edit_file/记忆/skill 工具
│   ├── mcp.py              # MCP stdio 客户端
│   └── core.py             # Agent 主循环（编程助手身份 + 工具调用循环）
├── skills/                 # 6 个编程向 skill（见上表）
├── mcp_servers.json        # MCP 服务器配置（默认带零依赖演示服务器）
├── mcp_demo_server.py      # 演示用 MCP 服务器（now / calc）
├── agent_config.json       # LLM 配置（已配置 GLM-5.3-Flash，勿提交）
└── memory/                 # 运行时自动创建：记忆与摘要
```

## 快速开始

```powershell
cd "E:\乐知\代码agent"

# 自检（不调 LLM：验证记忆读写 / Skill / Shell / MCP 链路）
python run_agent.py --check

# 离线 Mock 演示完整链路（无需 API）
python run_agent.py --mock "帮我写一个个人介绍网页"

# 编程任务
python run_agent.py "帮我写一个介绍番茄炒蛋做法的个人网页，美观现代，保存到 output 目录"
python run_agent.py "写一个 Python 命令行待办事项工具，要求能增删查、支持完成标记，并运行验证"
python run_agent.py --session 20261120-201015 "继续刚才的项目，加上数据持久化"
```

`agent_config.json` 已配置智谱 OpenAI 兼容端点 + GLM-5.3-Flash；换模型只需改
`api_base / model / api_key` 三个字段（或用环境变量 `AGENT_API_BASE/KEY/MODEL`）。

## 交互命令

`/mem` 记忆状态 · `/skills` skill 列表 · `/tools` 工具列表 · `/compact` 立即压缩上下文 · `/exit` 退出

## 长期记忆（编程上下文）

- **持久化**：`memory/long_term.json`（类型：fact / preference / event / skill / code）、
  `memory/sessions/<id>.json`（每轮自动保存）、`memory/history/<id>.jsonl`（压缩原文归档）；
- **摘要**：上下文过长自动压缩早期轮次（原文归档不丢）；会话结束自动总结到 `summary.md`
  并提取长期记忆（技术栈偏好、项目约定、经验教训），`--no-learn` 关闭；
- **检索**：新会话注入"最近会话摘要 + 相关记忆"，agent 也可 `memory_search` 主动查。

## MCP 接入

`mcp_servers.json`（stdio 传输），默认含零依赖演示服务器 `demo`（`now`/`calc`）；
`mcp_servers.example.json` 有 filesystem/fetch 等真实服务器示例。服务器启动失败只跳过不阻断。

## Shell

Windows 默认 PowerShell（回退 cmd），可配 `"shell": "cmd|powershell|bash"`；
返回 exit code/stdout/stderr，超长截断；危险命令（格式化/关机/递归删盘等）命中即拦截。

## 扩展

- **新增工具**：在 `agent/tools.py` 用 `Tool(name, description, parameters, handler)` 定义并在
  `run_agent.build_registry` 注册，`parameters` 用 JSON Schema；
- **新增 skill**：`skills/<name>/SKILL.md`；
- **换模型**：任何 OpenAI 兼容 API。
