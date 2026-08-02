# -*- coding: utf-8 -*-
"""盘中规律选股器。

设计：继承 PatternScreener，通过注入 _TmpDBShim 把所有 self.db.get_bulk_daily_data
调用重定向到 tmp 库。算法逻辑（_score_and_filter / _compute_wash_score /
_apply_wash_score）零修改继承自父类。

- screen_intraday(): 跳过缓存/历史反推/concept_cache，直接接收实时成分股
- 数据源：stock_daily_intraday_tmp（不含 today，today 由 1min 聚合单独写）

不重写父类的 _score_and_filter / _apply_wash_score —— 这是有意为之：
确保盘中版与盘后版算法 byte-by-byte 一致（用户要求"算法和 --pattern-screen 一样"）。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.services.pattern_screener import (
    PatternCandidate,
    PatternScreener,
    PatternScreenerConfig,
)

logger = logging.getLogger(__name__)


class _TmpDBShim:
    """伪装成 DatabaseManager，提供 get_bulk_daily_data 方法。

    PatternScreener 内部所有 self.db.get_bulk_daily_data(days, end_date) 调用
    都会被重定向到 tmp 库。忽略 days/end_date 参数 —— tmp 库只含 15 天 + today。
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    def get_bulk_daily_data(self, days: int = 15, end_date: Optional[str] = None) -> pd.DataFrame:
        with self.engine.connect() as conn:
            df = pd.read_sql_query(
                text(
                    "SELECT code, date, open, high, low, close, volume, amount, pct_chg "
                    "FROM stock_daily_intraday_tmp ORDER BY code, date"
                ),
                conn,
            )
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df


class IntradayPatternScreener(PatternScreener):
    """盘中规律选股器。

    与父类 PatternScreener 的区别：
    1. __init__ 接受 engine（tmp 库），通过 _TmpDBShim 注入到 self.db
    2. 新增 screen_intraday() 入口：跳过缓存/历史反推/concept_cache，直接接收实时成分股
    3. 父类 screen() 仍可调用，但盘中场景不应使用（会去读 concept_cache）
    """

    def __init__(
        self,
        engine: Engine,
        *,
        theme_universe_provider: Optional[Callable[[], Dict[str, List[str]]]] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._engine = engine
        self._shim_db = _TmpDBShim(engine)
        super().__init__(
            db=self._shim_db,
            theme_universe_provider=theme_universe_provider,
            stock_name_provider=stock_name_provider,
        )

    def screen_intraday(
        self,
        config: Optional[PatternScreenerConfig] = None,
        stock_themes: Optional[Dict[str, List[str]]] = None,
        stock_names: Optional[Dict[str, str]] = None,
    ) -> List[PatternCandidate]:
        """盘中选股入口。

        Args:
            config: 规律选股配置
            stock_themes: 实时热点概念的成分股 → {code: [题材]}
            stock_names: {code: name} 名称映射

        Returns:
            List[PatternCandidate]，已含规律分 + 洗盘分
        """
        config = config or PatternScreenerConfig()
        if not stock_themes:
            logger.warning("[IntradayPatternScreen] 未传入实时成分股")
            return []

        df = self._shim_db.get_bulk_daily_data()
        if df.empty:
            logger.warning("[IntradayPatternScreen] tmp 库 stock_daily_intraday_tmp 无数据")
            return []

        logger.info(
            "[IntradayPatternScreen] 开始评分: %d 只候选股, %d 行日线",
            len(stock_themes), len(df),
        )
        candidates = self._score_and_filter(
            df=df,
            stock_themes=stock_themes,
            stock_names=stock_names or {},
            config=config,
            date_key="",
        )
        logger.info(
            "[IntradayPatternScreen] 评分完成: %d 只入选",
            len(candidates),
        )
        return candidates
