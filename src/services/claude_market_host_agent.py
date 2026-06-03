# -*- coding: utf-8 -*-
"""Claude market host for intraday stock-picking push reports."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from src.agent.llm_adapter import LLMToolAdapter

logger = logging.getLogger(__name__)


@dataclass
class ClaudeHostReport:
    content: str
    used_claude: bool
    model: str = ""
    error: Optional[str] = None


class ClaudeMarketHostAgent:
    """Generate a Claude-hosted explanation without using the legacy ReAct agent."""

    def __init__(self, *, config: Any, llm_adapter: Optional["LLMToolAdapter"] = None) -> None:
        self.config = config
        self._llm_adapter = llm_adapter

    @property
    def llm_adapter(self) -> "LLMToolAdapter":
        if self._llm_adapter is None:
            from src.agent.llm_adapter import LLMToolAdapter

            self._llm_adapter = LLMToolAdapter(self.config)
        return self._llm_adapter

    def build_report(self, payload: Dict[str, Any], fallback_content: str) -> ClaudeHostReport:
        if not getattr(self.config, "intraday_pick_use_claude", True):
            return ClaudeHostReport(content=fallback_content, used_claude=False)
        if not self.llm_adapter.is_available:
            return ClaudeHostReport(content=fallback_content, used_claude=False, error="llm_unavailable")

        try:
            response = self.llm_adapter.call_text(
                [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(payload)},
                ],
                temperature=0.2,
                max_tokens=int(getattr(self.config, "intraday_pick_claude_max_tokens", 1200) or 1200),
                timeout=float(getattr(self.config, "intraday_pick_claude_timeout_seconds", 60) or 60),
            )
        except Exception as exc:
            logger.warning("[IntradayPick] Claude 主持报告生成失败: %s", exc)
            return ClaudeHostReport(content=fallback_content, used_claude=False, error=str(exc))

        content = (response.content or "").strip()
        if not content or response.provider == "error":
            error = content or "empty_llm_response"
            return ClaudeHostReport(content=fallback_content, used_claude=False, model=response.model, error=error)
        return ClaudeHostReport(content=content, used_claude=True, model=response.model)

    @staticmethod
    def _system_prompt() -> str:
        return "\n".join(
            [
                "你是 daily_stock_analysis 的 Claude 市场分析主持人。",
                "热点扩散规则引擎负责选股，你负责解释题材阶段、候选排序、风险和观察条件。",
                "必须基于输入 JSON，不得编造新闻、行情、板块或指标。",
                "不得下单，不得承诺收益，不得给确定性买卖指令。",
                "输出适合通知渠道推送的 markdown，中文，短而清楚。"
                "必须包含：核心结论、题材状态、候选排序、主要风险、下一步观察、数据说明。",
            ]
        )

    @staticmethod
    def _user_prompt(payload: Dict[str, Any]) -> str:
        return "请根据以下盘中选股结果生成通知推送报告：\n" + json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )
