---
name: shell-helper
description: 开发环境与工程操作。当用户要求安装依赖/管理虚拟环境、运行测试与脚本、git 提交与分支操作、查看进程端口、批量处理项目文件时使用。
---

# 开发环境操作 Skill

## 1. 安全第一
- 涉及删除/覆盖/移动的批量操作，先用只读命令（`Get-ChildItem`/`git status`）列出将受影响的对象，确认范围后再执行；
- 工作区外的危险操作（格式化、关机、递归删盘、改注册表）一律拒绝并说明原因。

## 2. 执行
- Windows 用 PowerShell 语法；命令在项目工作目录中执行；
- 一条命令做一件事；长输出加截断（如 `Select-Object -First 20`）；
- 依赖安装优先虚拟环境：`python -m venv .venv` 后用 `.venv\Scripts\python -m pip install ...`。

## 3. git 操作
- 任何操作前先 `git status` / `git diff` 查看现场；
- 只做本地操作（add/commit/branch/checkout）；不 push、不动远端；
- 提交信息简洁说明"为什么改"，一行一个提交一个主题。

## 4. 回报
最终答复：执行了哪些命令、改变了什么、如何撤销（如适用）。
