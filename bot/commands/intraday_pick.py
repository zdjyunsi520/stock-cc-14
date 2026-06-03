# -*- coding: utf-8 -*-
"""盘中选股命令。"""

import logging
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)


class IntradayPickCommand(BotCommand):
    """手动触发热点扩散盘中选股。"""

    @property
    def name(self) -> str:
        return "intraday_pick"

    @property
    def aliases(self) -> List[str]:
        return ["ipick", "盘中选股", "热点选股"]

    @property
    def description(self) -> str:
        return "手动运行热点扩散盘中选股"

    @property
    def usage(self) -> str:
        return "/intraday_pick"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行盘中选股命令。"""
        try:
            from src.services.intraday_pick_scheduler import IntradayPickScheduler

            result = IntradayPickScheduler().run_manual()
            content = str(result.get("content") or "")
            if not content:
                return BotResponse.error_response("盘中选股未生成有效报告")
            return BotResponse.markdown_response(content, at_user=False)
        except Exception as exc:
            logger.error("[IntradayPickCommand] 盘中选股执行失败: %s", exc)
            logger.exception(exc)
            return BotResponse.error_response(f"盘中选股执行失败: {str(exc)[:100]}")
