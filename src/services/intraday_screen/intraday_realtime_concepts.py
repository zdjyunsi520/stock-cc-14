# -*- coding: utf-8 -*-
"""盘中实时热点概念 + 成分股获取。

与 --cache-concepts / --pattern-screen 共用同一方法 get_hot_theme_universe，
保证算法完全一致。唯一差异：不写入 concept_cache JSON，结果只在内存。

之前自实现的 get_realtime_hot_themes / get_realtime_theme_members 已废弃，
因为过滤规则（ST/代码前缀）与 get_hot_theme_universe 不一致会导致选股池偏差。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from sqlalchemy import text

logger = logging.getLogger(__name__)


def get_realtime_theme_universe(
    theme_n: int = 20,
) -> Tuple[Dict[str, List[str]], Dict[str, str], List[Tuple[str, float]]]:
    """与 PatternScreener.cache_todays_concepts 共用 get_hot_theme_universe。

    流程（与 --cache-concepts 完全一致）：
        1. get_hot_theme_universe(n, max_members_per_theme=100)
           - 内部调 get_concept_rankings(n) 拿热点概念
           - 对每个概念调 get_board_members 拿成分股
           - 过滤：is_bse_code / is_kc_cy_stock / is_st_stock
        2. get_concept_rankings(n) 复用（命中 300s 缓存）→ hot_themes 带 pct，用于 banner
        3. sync_state 表查 stock_names（仅用于报告显示，不影响算法）

    Args:
        theme_n: 取前 N 个热点概念（与 PatternScreenerConfig.concept_top_n 一致）

    Returns:
        (stock_themes, stock_names, hot_themes)
        - stock_themes: {code: [theme_name, ...]}
        - stock_names: {code: name}
        - hot_themes: [(theme_name, change_pct), ...] 按 get_concept_rankings 顺序
    """
    from data_provider.base import get_data_fetcher_manager

    try:
        mgr = get_data_fetcher_manager()
    except Exception as exc:
        logger.error("[IntradayConcepts] data_manager 初始化失败: %s", exc)
        return {}, {}, []

    # === 与 PatternScreener.cache_todays_concepts 同款方法、同款参数 ===
    try:
        universe = mgr.get_hot_theme_universe(n=theme_n, max_members_per_theme=100)
    except Exception as exc:
        logger.error("[IntradayConcepts] get_hot_theme_universe 失败: %s", exc)
        return {}, {}, []

    if not universe:
        logger.warning("[IntradayConcepts] get_hot_theme_universe 返回空")
        return {}, {}, []

    # === hot_themes（带 pct）：get_hot_theme_universe 内部已调过 get_concept_rankings，
    #     同进程 300s 辅助缓存命中，这里再调拿到的是同一份数据，保证与 universe 概念对齐
    hot_themes: List[Tuple[str, float]] = []
    try:
        result = mgr.get_concept_rankings(theme_n)
        up_list = result[0] if result else []
        for r in up_list[:theme_n]:
            name = r.get("name") or r.get("板块名称")
            pct = r.get("change_pct") or r.get("涨跌幅") or 0.0
            if name:
                try:
                    hot_themes.append((str(name), float(pct)))
                except (TypeError, ValueError):
                    hot_themes.append((str(name), 0.0))
    except Exception as exc:
        logger.warning("[IntradayConcepts] get_concept_rankings 失败: %s", exc)

    # === stock_names（仅显示用）：从 stock_daily_sync_state 批量查 code_name ===
    stock_names = _lookup_stock_names(list(universe.keys()))

    logger.info(
        "[IntradayConcepts] 共取得 %d 只候选股，%d 个热点概念：%s",
        len(universe), len(hot_themes),
        ", ".join(t[0] for t in hot_themes),
    )
    return universe, stock_names, hot_themes


def _lookup_stock_names(codes: List[str]) -> Dict[str, str]:
    """从 stock_daily_sync_state 批量查 code → code_name。仅用于报告显示。"""
    if not codes:
        return {}
    try:
        from src.storage import DatabaseManager

        db = DatabaseManager()
        placeholders = ",".join(f":c{i}" for i in range(len(codes)))
        params = {f"c{i}": c for i, c in enumerate(codes)}
        with db.get_session() as session:
            rows = session.execute(
                text(
                    f"SELECT code, code_name FROM stock_daily_sync_state "
                    f"WHERE code IN ({placeholders})"
                ),
                params,
            ).fetchall()
        return {r[0]: (r[1] or "") for r in rows}
    except Exception as exc:
        logger.warning("[IntradayConcepts] 查 stock_names 失败: %s", exc)
        return {}
