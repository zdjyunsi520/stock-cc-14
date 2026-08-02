# -*- coding: utf-8 -*-
"""盘中持续热点筛选。

算法精神与 PatternScreener._find_persistent_themes 完全一致，唯一差异：
- 盘后：universe 从 concept_cache JSON 读
- 盘中：universe 从实时 API 拿（由调用方传入，避免盘中数据落盘污染历史）

universe 数据结构两端相同：{code: [concept_name, ...]}。
其他逻辑（lookback/recent_days/TOP5/min_days/≥3% 阈值）完全复用盘后版本，
通过 theme_universe_provider 注入 PatternScreener，保证永不脱节。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def filter_persistent_hot_themes(
    theme_universe: Dict[str, List[str]],
    *,
    date_key: str = "",
) -> List[Tuple[str, int]]:
    """盘中持续热点筛选（算法与盘后完全一致）。

    Args:
        theme_universe: 实时 API 返回的 {code: [concept_name, ...]}。
            由 get_realtime_theme_universe() 返回的 stock_themes 直接传入。
        date_key: 截止日期 YYYYMMDD（盘中场景通常留空，用最新）

    Returns:
        [(concept_name, hit_days), ...] 按 hit_days 降序，最多 10 个。
        算法细节（lookback_days=15 / recent_days=5 / daily_top=5 / min_days=3 / pct≥3%）
        全部沿用 PatternScreenerConfig 默认值，与盘后一致。
    """
    if not theme_universe:
        logger.warning("[IntradayPersistent] 未传入 universe（实时 API 返回空）")
        return []

    from src.services.pattern_screener import PatternScreener, PatternScreenerConfig

    screener = PatternScreener(
        theme_universe_provider=lambda: dict(theme_universe),
    )
    config = PatternScreenerConfig()
    persistent = screener._find_persistent_themes(config, date_key=date_key)

    if persistent:
        detail = [(c, n) for c, n in persistent]
        logger.info("[IntradayPersistent] 持续热点（算法=盘后）: %s", detail)
    else:
        logger.warning(
            "[IntradayPersistent] 无概念达到 min_days 持续（universe %d 只 → 持续 0 个）",
            len(theme_universe),
        )
    return persistent
