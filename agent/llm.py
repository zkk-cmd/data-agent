"""OpenAI 兼容 LLM 客户端（纯标准库 urllib 实现，支持工具调用）。

chat(messages, tools) -> (assistant_message_dict, usage_dict_or_None)

assistant_message_dict 形如：
  {"role": "assistant", "content": "...", "tool_calls": [{"id":..., "type":"function",
    "function": {"name":..., "arguments": "<json 字符串>"}}]}
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .config import Config


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        body: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        url = self.cfg.api_base + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.cfg.api_key,
        }

        last_err: Optional[str] = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_sec) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                choice = payload["choices"][0]
                msg = choice["message"]
                # 规整字段，保证可直接放回 messages
                clean = {"role": "assistant", "content": msg.get("content") or ""}
                if msg.get("tool_calls"):
                    clean["tool_calls"] = msg["tool_calls"]
                if msg.get("reasoning_content"):
                    clean["reasoning_content"] = msg["reasoning_content"]
                usage = payload.get("usage")
                return clean, usage
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:  # noqa: BLE001
                    pass
                last_err = "HTTP {}: {}".format(e.code, detail)
                if e.code in (401, 403):
                    raise LLMError(
                        last_err
                        + "\n提示：API 令牌无效/过期。请确认：① api_key 与 api_base 配套"
                          "（智谱开放平台的 key 对应 https://open.bigmodel.cn/api/paas/v4；"
                          "若是其他平台/中转签发的 key，需填写该平台的 base_url）；"
                          "② key 未被重置或删除。"
                    )
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError(last_err)
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    ConnectionError, json.JSONDecodeError) as e:
                last_err = str(e)
                time.sleep(2 ** attempt)
                continue
        raise LLMError("LLM 请求失败（已重试 3 次）：{}".format(last_err))


class MockLLM:
    """离线演示用的脚本化 LLM：按固定剧本调用工具，验证整条 agent 链路。

    剧本：use_skill("web-dev") -> write_file(output/xxx.html) -> 最终答复。
    同时能响应会话结束时的"记忆提取/摘要"提示词。
    """

    PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MiniAgent-Code 演示页</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: "Microsoft YaHei", system-ui, sans-serif;
         background: linear-gradient(135deg, #0f172a 0%, #1e293b 60%, #312e81 100%); color:#e2e8f0; }}
  .card {{ background: rgba(30,41,59,.75); border:1px solid rgba(148,163,184,.25);
           border-radius:18px; padding:48px 40px; max-width:640px; text-align:center;
           box-shadow:0 20px 60px rgba(0,0,0,.45); backdrop-filter: blur(8px); }}
  h1 {{ margin:0 0 12px; font-size:32px; background:linear-gradient(90deg,#38bdf8,#a78bfa);
        -webkit-background-clip:text; background-clip:text; color:transparent; }}
  p  {{ line-height:1.8; color:#94a3b8; }}
  .badge {{ display:inline-block; margin-top:18px; padding:6px 14px; border-radius:999px;
            background:rgba(56,189,248,.15); color:#7dd3fc; font-size:13px; }}
</style>
</head>
<body>
  <main class="card">
    <h1>你好，来自 MiniAgent-Code 👋</h1>
    <p>这个页面由 MiniAgent-Code（Mock 模式）自动生成，用于验证：<br>
       长期记忆 · Skill 自主选择 · Shell 执行 · MCP 接入</p>
    <span class="badge">生成时间：{now}</span>
  </main>
</body>
</html>
"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._steps = 0

    def chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self._steps += 1
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = str(m.get("content") or "")
                break
        # 会话结束的辅助提示词（无工具）
        if not tools:
            if "提取长期记忆" in last_user:
                return ({"role": "assistant",
                         "content": '[{"type":"event","content":"用户在 Mock 演示中让 agent 生成一个网页","tags":["演示","网页"]}]'},
                        None)
            if "总结" in last_user:
                return ({"role": "assistant",
                         "content": "Mock 会话：agent 按用户要求自主选择了 web-dev skill 并生成了演示网页。"}, None)
            return ({"role": "assistant", "content": "（Mock 回应）" + last_user[:60]}, None)

        # 剧本推进
        already = [tc["function"]["name"] for m in messages
                   for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant"]
        if "use_skill" not in already:
            return ({"role": "assistant", "content": None, "tool_calls": [{
                "id": "mock-1", "type": "function",
                "function": {"name": "use_skill", "arguments": '{"skill_name": "web-dev"}'}}]}, None)
        if "write_file" not in already:
            import time as _t
            fname = "output/mock-webpage-{}.html".format(_t.strftime("%H%M%S"))
            return ({"role": "assistant", "content": None, "tool_calls": [{
                "id": "mock-2", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": fname,
                                 "content": self.PAGE_TMPL.format(now=_t.strftime("%Y-%m-%d %H:%M:%S"))},
                                 ensure_ascii=False)}}]}, None)
        return ({"role": "assistant",
                 "content": "已完成（Mock 离线演示）：\n"
                            "1. 自主选择并加载了 web-dev skill；\n"
                            "2. 生成网页文件 output/ 目录下 mock-webpage-*.html；\n"
                            "3. 会话记忆与摘要将在退出时写入 memory/ 目录。\n"
                            "配置真实 API Key 后即可换成真正的 LLM。"}, None)
