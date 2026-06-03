# -*- coding: utf-8 -*-
"""Restricted Claude bridge context for bot stock questions.

This module does not expose Claude Code CLI, shell, file writes, or arbitrary
filesystem access. It only builds a small read-only context block from approved
project guidance files so the in-app Agent can answer more like the local
project host.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from src.services.anthropic_direct_client import AnthropicDirectClient

logger = logging.getLogger(__name__)


class ClaudeBridgeService:
    """Build safe project context for the in-app Claude host."""

    _MAX_FILE_CHARS = 3000
    _MAX_CONTEXT_CHARS = 9000
    _MEMORY_PATTERNS = ("project_*.md", "feedback_*.md", "reference_*.md")
    _SENSITIVE_PARTS = {
        ".env",
        "data",
        "logs",
        "node_modules",
        "__pycache__",
        ".git",
    }
    _SENSITIVE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".p12"}

    def __init__(self, project_root: Optional[Path] = None, memory_root: Optional[Path] = None) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.memory_root = memory_root or self._default_memory_root(self.project_root)

    @staticmethod
    def _default_memory_root(project_root: Path) -> Path:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(project_root)).strip("-")
        return Path.home() / ".claude" / "projects" / slug / "memory"

    def build_stock_user_message(self, stock_code: str, skill_id: str = "", skill_text: str = "") -> str:
        """Compose the user prompt for a stock question with restricted context."""
        parts = [
            f"请以 daily_stock_analysis 的 Claude 只读市场分析主持人身份分析股票 {stock_code}。",
            "你可以使用项目内已注册的行情、新闻、技术面、历史分析等只读工具。",
            "禁止下单、写入持仓、承诺收益、读取或泄露密钥，不要假装拥有未提供的数据。",
        ]
        if skill_id:
            parts.append(f"优先使用策略技能: {skill_id}。")
        if skill_text:
            parts.append(f"用户补充要求: {skill_text}")

        context = self.build_restricted_context()
        if context:
            parts.append("以下是只读项目上下文，请作为行为边界和协作偏好使用，不要逐字复述:")
            parts.append(context)
        return "\n\n".join(parts)

    def build_stock_answer(self, stock_code: str, skill_id: str = "", skill_text: str = "", config=None) -> str:
        """Ask Claude directly through Anthropic Messages API."""
        client = AnthropicDirectClient(config=config)
        user_message = self.build_stock_user_message(stock_code, skill_id=skill_id, skill_text=skill_text)
        response = client.create_message(
            user_message,
            system=(
                "你是 daily_stock_analysis 的 Claude 只读市场分析主持人。"
                "只做复盘、问股、实时分析和情景推演；不下单、不写持仓、不承诺收益。"
            ),
            max_tokens=1800,
            temperature=0.2,
        )
        return response.content.strip()

    def build_restricted_context(self) -> str:
        """Return a bounded, non-sensitive context block."""
        chunks = []
        for title, path in self._iter_context_files():
            content = self._read_safe_text(path)
            if content:
                chunks.append(f"## {title}\n{content}")

        if not chunks:
            return ""

        context = "\n\n".join(chunks)
        if len(context) > self._MAX_CONTEXT_CHARS:
            return context[: self._MAX_CONTEXT_CHARS] + "\n...[context truncated]"
        return context

    def _iter_context_files(self) -> Iterable[tuple[str, Path]]:
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        project_claude = self.project_root / "CLAUDE.md"
        yield "Global CLAUDE.md", user_claude
        yield "Project CLAUDE.md", project_claude

        memory_index = self.memory_root / "MEMORY.md"
        yield "Memory index", memory_index

        if self.memory_root.exists():
            for pattern in self._MEMORY_PATTERNS:
                for path in sorted(self.memory_root.glob(pattern))[:5]:
                    yield f"Memory {path.name}", path

    def _read_safe_text(self, path: Path) -> str:
        if not self._is_safe_context_path(path):
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.debug("Claude bridge context skipped %s: %s", path, exc)
            return ""
        if not text:
            return ""
        return text[: self._MAX_FILE_CHARS]

    def _is_safe_context_path(self, path: Path) -> bool:
        normalized_parts = {part.lower() for part in path.parts}
        if normalized_parts & self._SENSITIVE_PARTS:
            return False
        if path.suffix.lower() in self._SENSITIVE_SUFFIXES:
            return False
        name = path.name.lower()
        if any(marker in name for marker in ("secret", "token", "credential", "password")):
            return False
        return path.name in {"CLAUDE.md", "MEMORY.md"} or path.parent == self.memory_root


@lru_cache(maxsize=1)
def get_claude_bridge_service() -> ClaudeBridgeService:
    return ClaudeBridgeService()
