# -*- coding: utf-8 -*-
"""MA10 回踩选股器 — 股价在10日线附近回踩不破、放量、关联热点概念。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class MA10PullbackCriteria:
    ma_distance_max_pct: float = 5.0      # 收盘价距MA10最大百分比
    volume_ratio_min: float = 1.2          # 最小量比
    lookback_days: int = 15                # 取多少天数据计算
    concept_top_n: int = 20                # 取前N个热点概念
    max_candidates: int = 30               # 最多返回候选数
    prev_days_no_break: int = 3            # 前N日最低都不破MA10


@dataclass
class MA10PullbackCandidate:
    code: str
    name: str = ""
    close: float = 0.0
    ma10: float = 0.0
    dist_ma10_pct: float = 0.0
    volume_ratio: float = 0.0
    change_pct: float = 0.0
    themes: List[str] = field(default_factory=list)
    theme_top_rank: int = 999
    score: float = 0.0


class MA10PullbackScreener:
    """全市场 MA10 回踩选股。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        theme_universe_provider: Optional[Callable[[], Dict[str, Sequence[str]]]] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._theme_universe_provider = theme_universe_provider
        self._stock_name_provider = stock_name_provider

    def screen(self, criteria: Optional[MA10PullbackCriteria] = None) -> List[MA10PullbackCandidate]:
        criteria = criteria or MA10PullbackCriteria()

        # Step 1: 批量加载日线
        df = self.db.get_bulk_daily_data(days=criteria.lookback_days)
        if df.empty:
            logger.warning("[MA10Screen] 无日线数据")
            return []

        # Step 2: 计算指标
        metrics_df = self._compute_metrics(df)
        if metrics_df.empty:
            return []

        # Step 3: MA10 回踩 + 放量过滤
        filtered = self._filter_pullback_volume(metrics_df, criteria)
        if filtered.empty:
            logger.info("[MA10Screen] 无符合条件的股票")
            return []

        # Step 4: 关联概念
        theme_universe = self._load_theme_universe(criteria.concept_top_n)
        candidates = self._build_candidates(filtered, theme_universe, criteria)

        logger.info("[MA10Screen] 筛选结果: %d 只", len(candidates))
        return candidates

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
        """按 code 分组计算 MA10、量比等指标。"""
        results = []
        for code, group in df.groupby("code"):
            # 排除创业板(300/301)、科创板(688)、北交所
            if code.startswith(("300", "301", "688", "4", "8")):
                continue
            g = group.sort_values("date").copy()
            if len(g) < 10:
                continue

            close = g["close"]
            volume = g["volume"]

            g["ma10"] = close.rolling(window=10, min_periods=10).mean()
            avg_vol_5 = volume.rolling(window=5, min_periods=5).mean().shift(1)
            g["vol_ratio"] = (volume / avg_vol_5).where(avg_vol_5 > 0)

            # 取最后1行作为当前状态
            last = g.iloc[-1]

            # 前3日最低价是否都在各自MA10上方
            prev_n = g.iloc[-4:-1]  # 前3天
            prev_lows_above_ma10 = bool((prev_n["low"] >= prev_n["ma10"]).all()) if len(prev_n) == 3 else False

            results.append({
                "code": code,
                "close": last["close"],
                "ma10": last["ma10"],
                "prev_close": g.iloc[-2]["close"],
                "prev_lows_above_ma10": prev_lows_above_ma10,
                "volume_ratio": last["vol_ratio"],
                "change_pct": last["pct_chg"],
                "volume": last["volume"],
            })

        if not results:
            return pd.DataFrame()
        return pd.DataFrame(results)

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_pullback_volume(df: pd.DataFrame, criteria: MA10PullbackCriteria) -> pd.DataFrame:
        """MA10 回踩 + 放量过滤。"""
        c = criteria

        df = df.dropna(subset=["close", "ma10", "volume_ratio", "prev_close"])

        # 收盘价 ≥ MA10（不破）
        mask_no_break = df["close"] >= df["ma10"]

        # 收盘价 ≤ MA10 × (1 + max_pct)（在均线附近）
        mask_near = df["close"] <= df["ma10"] * (1 + c.ma_distance_max_pct / 100)

        # 今日收盘 < 昨日收盘（回踩）
        mask_pullback = df["close"] < df["prev_close"]

        # 前N日最低都在各自MA10上方
        mask_prev_above = df["prev_lows_above_ma10"]

        # 放量
        mask_volume = df["volume_ratio"] >= c.volume_ratio_min

        filtered = df[mask_no_break & mask_near & mask_pullback & mask_prev_above & mask_volume].copy()
        filtered["dist_ma10_pct"] = (filtered["close"] / filtered["ma10"] - 1) * 100
        return filtered

    # ------------------------------------------------------------------
    # Theme universe
    # ------------------------------------------------------------------

    def _load_theme_universe(self, top_n: int) -> Dict[str, List[str]]:
        """获取热点概念→成分股映射。盘前用上一交易日缓存，盘中用实时热点。"""
        if self._theme_universe_provider:
            return dict(self._theme_universe_provider())

        try:
            from src.core.trading_calendar import get_effective_trading_date, build_market_phase_context
            effective_date = get_effective_trading_date("cn")
            context = build_market_phase_context(market="cn", trigger_source="ma10_screen")
            is_live = context.is_market_open_now is True
            label = "盘中实时" if is_live else f"上一交易日({effective_date})"
            logger.info("[MA10Screen] 热点来源: %s", label)
        except Exception:
            pass

        try:
            from data_provider.base import DataFetcherManager
            manager = DataFetcherManager()
            universe = manager.get_hot_theme_universe(n=top_n, max_members_per_theme=100)
            if universe:
                logger.info("[MA10Screen] 热点概念加载完成: %d 只股票有概念标注", len(universe))
                return dict(universe)
        except Exception as exc:
            logger.warning("[MA10Screen] 获取概念板块失败: %s", exc)

        return {}

    # ------------------------------------------------------------------
    # Build candidates
    # ------------------------------------------------------------------

    def _build_candidates(
        self,
        filtered: pd.DataFrame,
        theme_universe: Dict[str, List[str]],
        criteria: MA10PullbackCriteria,
    ) -> List[MA10PullbackCandidate]:
        """关联概念、评分、排序。"""
        # 如果有概念数据，只保留有概念的股票
        has_themes = bool(theme_universe)

        candidates = []
        for _, row in filtered.iterrows():
            code = row["code"]
            themes = list(theme_universe.get(code, []))

            # 概念排名（越小越好）
            theme_rank = 999
            if themes and has_themes:
                theme_rank = 1  # 至少属于一个热点概念

            # 综合评分：量比权重 + 距均线距离权重 + 概念加分
            vol_score = min(row["volume_ratio"] / 3.0, 1.0) * 40  # 量比贡献最多40分
            dist_score = max(0, 1 - abs(row["dist_ma10_pct"]) / 3.0) * 30  # 越贴近均线分越高
            theme_score = (1 - min(theme_rank, 20) / 20) * 30 if theme_rank < 999 else 0
            score = vol_score + dist_score + theme_score

            name = self._get_stock_name(code)

            candidates.append(MA10PullbackCandidate(
                code=code,
                name=name,
                close=round(row["close"], 2),
                ma10=round(row["ma10"], 2),
                dist_ma10_pct=round(row["dist_ma10_pct"], 2),
                volume_ratio=round(row["volume_ratio"], 2),
                change_pct=round(row.get("change_pct", 0) or 0, 2),
                themes=themes,
                theme_top_rank=theme_rank,
                score=round(score, 1),
            ))

        # 排序
        candidates.sort(key=lambda c: (-c.score, -c.volume_ratio))
        return candidates[:criteria.max_candidates]

    def _get_stock_name(self, code: str) -> str:
        if self._stock_name_provider:
            return self._stock_name_provider(code)
        try:
            from data_provider.base import DataFetcherManager
            return DataFetcherManager().get_stock_name(code) or ""
        except Exception:
            return ""
