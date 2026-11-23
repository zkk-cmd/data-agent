"""Skill 系统：从 skills/ 目录发现 skill，注入目录供模型自主选择，按需加载全文。

skill 目录结构：
  skills/<name>/SKILL.md
SKILL.md 使用 YAML 风格 frontmatter（--- 包裹），必需字段：
  name: <skill 名，与目录名一致>
  description: <一句话描述 + 何时使用（模型据此自主判断是否调用）>
正文为该 skill 的详细执行指令。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_FILE = "SKILL.md"


class Skill:
    def __init__(self, name: str, description: str, path: Path):
        self.name = name
        self.description = description
        self.path = path
        self._body: Optional[str] = None

    def load_body(self) -> str:
        if self._body is None:
            raw = self.path.read_text(encoding="utf-8")
            # 去掉 frontmatter，保留正文
            m = re.match(r"\s*---.*?---\s*(.*)$", raw, re.S)
            self._body = (m.group(1) if m else raw).strip()
        return self._body


class SkillManager:
    def __init__(self, dirs: List[Path]):
        self.dirs = [d for d in dirs if d.exists()]
        self.skills: Dict[str, Skill] = {}
        self.rescan()

    def rescan(self) -> None:
        self.skills = {}
        for d in self.dirs:
            for md in sorted(d.glob("*/" + SKILL_FILE)):
                meta = self._parse_frontmatter(md)
                if not meta:
                    continue
                name = meta.get("name") or md.parent.name
                self.skills[name] = Skill(name=name, description=meta.get("description", ""), path=md)

    @staticmethod
    def _parse_frontmatter(path: Path) -> Optional[Dict[str, str]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n", raw, re.S)
        if not m:
            return None
        meta: Dict[str, str] = {}
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith("#"):
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
        return meta if meta else None

    # ------------------------------------------------------------------ #
    def catalog_block(self) -> str:
        """注入系统提示词的 skill 目录（模型据此自主选择）。"""
        if not self.skills:
            return ""
        lines = []
        for s in self.skills.values():
            lines.append("- {}: {}".format(s.name, s.description))
        return "\n".join(lines)

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def load_into_prompt(self, name: str) -> str:
        """返回加载 skill 后应作为工具结果注入的完整文本。"""
        s = self.skills.get(name)
        if not s:
            avail = ", ".join(self.skills.keys()) or "（无）"
            raise KeyError("未找到 skill '{}'. 可用 skill: {}".format(name, avail))
        body = s.load_body()
        return (
            "【Skill 已加载：{}】\n来源：{}\n\n以下是该 skill 的执行指令，"
            "请严格遵循它们来完成当前用户任务：\n\n{}"
        ).format(s.name, s.path.name, body)

    def stats(self) -> str:
        if not self.skills:
            return "（未发现任何 skill）"
        return "\n".join(
            "- {} : {} ({})".format(s.name, s.description, s.path) for s in self.skills.values()
        )
