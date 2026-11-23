"""长期记忆系统：文件持久化 + 摘要 + 关键词检索。

目录结构（memory/）：
  long_term.json        长期记忆条目（事实/偏好/事件），可被 agent 通过工具写入
  summary.md            各会话的滚动摘要（每个会话结束时追加）
  sessions/<sid>.json   会话工作状态（含压缩摘要），每轮自动保存，可恢复
  history/<sid>.jsonl   被上下文压缩裁剪掉的原始消息归档（不丢失任何历史）

摘要体现在三层：
  1) 单条记忆本身是对信息的浓缩；
  2) 上下文压缩：对话过长时，把较早的轮次用 LLM 摘要替换，原文归档到 history；
  3) 会话摘要：会话结束时自动总结并追加到 summary.md，同时提取长期记忆。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_TYPES = ("fact", "preference", "event", "skill", "code")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class Memory:
    def __init__(self, root: Path, top_k: int = 6):
        self.root = root
        self.top_k = top_k
        self.sessions_dir = root / "sessions"
        self.history_dir = root / "history"
        for p in (root, self.sessions_dir, self.history_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.long_term_path = root / "long_term.json"
        self.summary_path = root / "summary.md"

    # ------------------------------------------------------------------ #
    # 长期记忆条目
    # ------------------------------------------------------------------ #
    def load_long_term(self) -> List[Dict[str, Any]]:
        if not self.long_term_path.exists():
            return []
        try:
            data = json.loads(self.long_term_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _save_long_term(self, entries: List[Dict[str, Any]]) -> None:
        # 上限 500 条，超出时丢弃最旧的非偏好条目
        if len(entries) > 500:
            keep = [e for e in entries if e.get("type") == "preference"][-100:]
            rest = [e for e in entries if e.get("type") != "preference"][-(500 - len(keep)):]
            entries = sorted(keep + rest, key=lambda e: e.get("time", ""))
        _atomic_write(self.long_term_path, json.dumps(entries, ensure_ascii=False, indent=2))

    def add_memory(self, content: str, mtype: str = "fact",
                   tags: Optional[List[str]] = None, source: str = "agent") -> Dict[str, Any]:
        content = str(content).strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if mtype not in MEMORY_TYPES:
            mtype = "fact"
        entries = self.load_long_term()
        # 去重：内容完全一致则更新时间戳
        for e in entries:
            if e.get("content") == content:
                e["time"] = _now()
                self._save_long_term(entries)
                return e
        entry = {
            "id": "m-{}-{}".format(time.strftime("%Y%m%d%H%M%S"), len(entries) + 1),
            "time": _now(),
            "type": mtype,
            "content": content,
            "tags": tags or [],
            "source": source,
        }
        entries.append(entry)
        self._save_long_term(entries)
        return entry

    def search(self, query: str, k: Optional[int] = None) -> List[Dict[str, Any]]:
        """关键词检索：中英混合分词（中文 2-gram + 英数词元），重叠度 + 标签加分 + 新近度加分。"""
        k = k or self.top_k
        entries = self.load_long_term()
        if not entries:
            return []
        q_tokens = self._tokenize(query)
        q_lower = query.strip().lower()
        now_ts = time.time()

        scored: List[tuple] = []
        for e in entries:
            text = e.get("content", "")
            tokens = self._tokenize(text)
            overlap = len(q_tokens & tokens)
            if q_lower and q_lower in text.lower():
                overlap += 2
            tag_hit = any(t.lower() in q_lower for t in e.get("tags", []) if t)
            if tag_hit:
                overlap += 2
            if overlap <= 0:
                continue
            recency = 0
            try:
                age_h = (now_ts - time.mktime(time.strptime(e["time"], "%Y-%m-%d %H:%M:%S"))) / 3600
                recency = max(0.0, 1.5 - age_h / (24 * 30))
            except Exception:  # noqa: BLE001
                pass
            scored.append((overlap + recency, e))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [e for _, e in scored[:k]]

    def recent(self, k: int = 8) -> List[Dict[str, Any]]:
        entries = self.load_long_term()
        return entries[-k:]

    @staticmethod
    def _tokenize(s: str) -> set:
        words = set(w.lower() for w in re.findall(r"[a-zA-Z0-9_]+", s))
        grams = set()
        for seg in re.findall(r"[\u4e00-\u9fff]+", s):
            if len(seg) == 1:
                grams.add(seg)
            for i in range(len(seg) - 1):
                grams.add(seg[i:i + 2])
        return words | grams

    # ------------------------------------------------------------------ #
    # 会话状态与持久化
    # ------------------------------------------------------------------ #
    def new_session(self) -> Dict[str, Any]:
        sid = time.strftime("%Y%m%d-%H%M%S")
        return {"id": sid, "created": _now(), "messages": [], "summaries": []}

    def save_session(self, sess: Dict[str, Any]) -> None:
        path = self.sessions_dir / (sess["id"] + ".json")
        _atomic_write(path, json.dumps(sess, ensure_ascii=False, indent=1))

    def load_session(self, sid: str) -> Optional[Dict[str, Any]]:
        path = self.sessions_dir / (sid + ".json")
        if not path.exists():
            return None
        sess = json.loads(path.read_text(encoding="utf-8"))
        sess.setdefault("summaries", [])
        return sess

    def list_sessions(self) -> List[str]:
        return sorted(p.stem for p in self.sessions_dir.glob("*.json"))

    # ------------------------------------------------------------------ #
    # 摘要层
    # ------------------------------------------------------------------ #
    def append_summary(self, sid: str, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        block = "\n## 会话 {}（{}）\n{}\n".format(sid, stamp, text.strip())
        with self.summary_path.open("a", encoding="utf-8") as f:
            f.write(block)

    def read_recent_summaries(self, n: int = 4) -> str:
        if not self.summary_path.exists():
            return ""
        text = self.summary_path.read_text(encoding="utf-8")
        blocks = [b for b in text.split("\n## ") if b.strip()]
        return "\n## ".join(b.strip() for b in blocks[-n:])

    def _archive(self, sid: str, messages: List[Dict[str, Any]]) -> None:
        path = self.history_dir / (sid + ".jsonl")
        with path.open("a", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ #
    # 上下文压缩（对话过长 -> 摘要替换 + 原文归档）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _msg_chars(msgs: List[Dict[str, Any]]) -> int:
        total = 0
        for m in msgs:
            total += len(str(m.get("content") or ""))
            if m.get("tool_calls"):
                total += len(json.dumps(m["tool_calls"], ensure_ascii=False))
        return total

    def maybe_compress(self, sess: Dict[str, Any], llm: Any, limit_chars: int) -> bool:
        """超过阈值时，把较早的对话压缩成一条摘要消息（保留最近 8 条）。

        会话里只存对话消息（system 提示词在每次 LLM 调用时动态构建），因此
        切点只选在 user 消息之前，保证 tool_call 与 tool 结果成对不被拆开。
        """
        msgs = sess["messages"]
        if self._msg_chars(msgs) <= limit_chars:
            return False
        tail_keep = 8
        cut = -1
        for i in range(len(msgs) - tail_keep, 0, -1):
            if msgs[i].get("role") == "user":
                cut = i
                break
        if cut <= 0:
            return False
        old_msgs = msgs[:cut]
        self._archive(sess["id"], old_msgs)
        summary = self._summarize_messages(old_msgs, llm)
        anchor = {
            "role": "user",
            "content": ("【自动压缩：以下是本会话更早对话的摘要，无需回应，等待用户后续输入】\n" + summary),
        }
        sess["messages"][:] = [anchor] + msgs[cut:]
        sess["summaries"].append({"time": _now(), "chars": self._msg_chars(old_msgs), "summary": summary})
        self.save_session(sess)
        return True

    def _summarize_messages(self, msgs: List[Dict[str, Any]], llm: Any) -> str:
        lines = []
        for m in msgs[:80]:
            role = m.get("role", "?")
            content = str(m.get("content") or "")
            if not content and m.get("tool_calls"):
                names = [tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]]
                content = "[调用工具: " + ", ".join(names) + "]"
            content = re.sub(r"\s+", " ", content)[:300]
            lines.append("{}: {}".format(role, content))
        prompt = (
            "请把下面这段 agent 工作对话压缩成不超过 300 字的要点摘要，"
            "保留：用户目标、已做的事（工具调用/生成的文件）、重要结论。直接输出摘要正文。\n\n" +
            "\n".join(lines)
        )
        try:
            msg, _ = llm.chat(
                [{"role": "system", "content": "你是对话摘要器，只输出摘要正文。"},
                 {"role": "user", "content": prompt}],
                tools=None, temperature=0.2, max_tokens=800,
            )
            text = (msg.get("content") or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            pass
        # LLM 不可用时的兜底摘要
        return "此前对话要点（兜底摘要）：\n" + "\n".join("- " + l[:120] for l in lines[:20])

    # ------------------------------------------------------------------ #
    # 会话结束学习：提取长期记忆 + 写会话摘要
    # ------------------------------------------------------------------ #
    def learn_from_session(self, sess: Dict[str, Any], llm: Any) -> Dict[str, Any]:
        result = {"memories": 0, "summary": ""}
        msgs = sess["messages"]
        if sum(1 for m in msgs if m.get("role") == "assistant") < 1:
            return result
        transcript_lines = []
        for m in msgs[-60:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = str(m.get("content") or "")[:400]
            if content:
                transcript_lines.append("{}: {}".format(role, content))
        if not transcript_lines:
            return result
        transcript = "\n".join(transcript_lines)[:12000]

        # 1) 提取长期记忆
        try:
            msg, _ = llm.chat(
                [{"role": "system", "content": "你是记忆提取器，只输出 JSON 数组，不要输出其他文字。"},
                 {"role": "user", "content": (
                     "从下面的对话中提取值得长期记住的信息（用户技术栈与代码风格偏好、项目结构与约定、"
                     "重要事实、经验教训），每条形如 {\"type\":\"fact|preference|event|skill|code\", "
                     "\"content\":\"...\", \"tags\":[...]}"
                     "，最多 8 条；没有值得记的就输出 []。\n\n对话：\n" + transcript
                 )}],
                tools=None, temperature=0.2, max_tokens=1200,
            )
            text = (msg.get("content") or "").strip()
            m = re.search(r"\[.*\]", text, re.S)
            if m:
                items = json.loads(m.group(0))
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict) and it.get("content"):
                        self.add_memory(
                            content=str(it["content"]),
                            mtype=str(it.get("type", "fact")),
                            tags=[str(t) for t in (it.get("tags") or [])][:6],
                            source="auto-" + sess["id"],
                        )
                        result["memories"] += 1
        except Exception:  # noqa: BLE001
            pass

        # 2) 会话摘要 -> summary.md
        try:
            msg, _ = llm.chat(
                [{"role": "system", "content": "你是会话总结器，只输出总结正文。"},
                 {"role": "user", "content": (
                     "用不超过 200 字总结这次会话：用户要做什么、结果如何、有哪些重要决定或产物。\n\n对话：\n" + transcript
                 )}],
                tools=None, temperature=0.3, max_tokens=600,
            )
            text = (msg.get("content") or "").strip()
            if text:
                result["summary"] = text
                self.append_summary(sess["id"], text)
        except Exception:  # noqa: BLE001
            pass
        return result

    # ------------------------------------------------------------------ #
    # 系统提示词中的记忆上下文块
    # ------------------------------------------------------------------ #
    def context_block(self, query: str = "") -> str:
        parts: List[str] = []
        entries = self.load_long_term()
        if entries:
            stats = "长期记忆库共 {} 条条目（memory/long_term.json）".format(len(entries))
            rel = self.search(query, self.top_k) if query.strip() else self.recent(self.top_k)
            if rel:
                lines = ["- [{}] {}（{}）".format(e["type"], e["content"], e["time"][:10]) for e in rel]
                parts.append(stats + "\n【与当前输入相关的记忆】\n" + "\n".join(lines))
            else:
                parts.append(stats)
        s = self.read_recent_summaries(4)
        if s:
            parts.append("【历史会话摘要】\n" + s)
        return "\n\n".join(parts)
