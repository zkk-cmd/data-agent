#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 演示服务器（纯标准库，stdio 传输）。

提供两个工具：
  now()           返回当前时间
  calc(expression) 安全计算四则/幂/括号表达式
在 mcp_servers.json 中配置：
  "mcpServers": { "demo": { "command": "python", "args": ["mcp_demo_server.py"] } }
"""
import ast
import json
import sys
import time

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "now",
        "description": "返回服务器当前本地时间（YYYY-MM-DD HH:MM:SS）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "calc",
        "description": "计算一个算术表达式，支持 + - * / // % ** 与括号",
        "inputSchema": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "如 (2+3)*4"}},
            "required": ["expression"],
        },
    },
]


def safe_calc(expr: str) -> str:
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
               ast.Pow, ast.USub, ast.UAdd, ast.Load)

    def _eval(node):
        if not isinstance(node, allowed):
            raise ValueError("不允许的表达: " + type(node).__name__)
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("仅支持数字常量")
        if isinstance(node, ast.BinOp):
            left, right = _eval(node.left), _eval(node.right)
            op = type(node.op)
            if op is ast.Add:
                return left + right
            if op is ast.Sub:
                return left - right
            if op is ast.Mult:
                return left * right
            if op is ast.Div:
                return left / right
            if op is ast.FloorDiv:
                return left // right
            if op is ast.Mod:
                return left % right
            if op is ast.Pow:
                return left ** right
        if isinstance(node, ast.UnaryOp):
            val = _eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -val
            if isinstance(node.op, ast.UAdd):
                return +val
        raise ValueError("无法计算")

    value = _eval(ast.parse(expr, mode="eval"))
    return str(value)


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    if rid is None:
        return None  # 通知，不回复
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "miniagent-demo-server", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "now":
                    text = time.strftime("%Y-%m-%d %H:%M:%S")
                elif name == "calc":
                    text = safe_calc(str(args.get("expression") or ""))
                else:
                    raise ValueError("未知工具: " + str(name))
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as e:  # noqa: BLE001
                result = {"content": [{"type": "text", "text": "计算失败: {}".format(e)}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not found: " + method}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32603, "message": "internal error: " + str(e)}}


def main():
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            out.write((json.dumps(resp, ensure_ascii=False) + "\n"))
            out.flush()


if __name__ == "__main__":
    main()
