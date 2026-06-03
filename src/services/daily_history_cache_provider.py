# -*- coding: utf-8 -*-
"""Cache-backed daily history provider for intraday screening."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

import pandas as pd

from data_provider.base import normalize_stock_code
from data_provider.tushare_fetcher import TushareFetcher
from src.config import get_config
from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)


class DailyHistoryCacheProvider:
    """Read daily bars from stock_daily and fill gaps with Tushare when available."""

    def __init__(self, *, rate_limit_per_minute: int = 45, stale_days: int = 5) -> None:
        self.db = DatabaseManager()
        self.stale_days = max(1, int(stale_days))
        self.fetcher: Optional[TushareFetcher] = None
        if get_config().tushare_token:
            self.fetcher = TushareFetcher(rate_limit_per_minute=rate_limit_per_minute)

    def __call__(self, code: str, days: int) -> Tuple[pd.DataFrame, str]:
        return self.get_daily_data(code, days)

    def get_daily_data(self, code: str, days: int) -> Tuple[pd.DataFrame, str]:
        code = normalize_stock_code(str(code or ""))
        if not code:
            return pd.DataFrame(), "invalid_code"

        cached = self._load_cached(code, days)
        if self._is_cache_enough(cached, days):
            return cached, "local_stock_daily"

        if self.fetcher is None or not self.fetcher.is_available():
            return cached, "local_stock_daily_insufficient" if not cached.empty else "local_stock_daily_empty"

        fetch_days = max(days, 40)
        try:
            fresh = self.fetcher.get_daily_data(code, days=fetch_days)
        except Exception as exc:
            logger.warning("[DailyHistoryCache] Tushare 补齐 %s 日线失败: %s", code, exc)
            return cached, "tushare_error_with_cache" if not cached.empty else "tushare_error"

        if fresh is None or fresh.empty:
            return cached, "tushare_empty_with_cache" if not cached.empty else "tushare_empty"

        self.db.save_daily_data(fresh, code, data_source="TushareFetcher")
        refreshed = self._load_cached(code, days)
        if not refreshed.empty:
            return refreshed, "tushare_cached_stock_daily"
        return fresh.tail(days).copy(), "tushare_stock_daily"

    def _load_cached(self, code: str, days: int) -> pd.DataFrame:
        variants = _code_variants(code)
        rows = []
        with self.db.get_session() as session:
            for variant in variants:
                result = (
                    session.query(StockDaily)
                    .filter(StockDaily.code == variant)
                    .order_by(StockDaily.date.desc())
                    .limit(max(days, 40))
                    .all()
                )
                rows.extend(result)
                if len(rows) >= days:
                    break

        if not rows:
            return pd.DataFrame()

        records = [row.to_dict() for row in rows]
        df = pd.DataFrame(records)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="first")
        df = df.sort_values("date").tail(days).reset_index(drop=True)
        return df

    def _is_cache_enough(self, df: pd.DataFrame, days: int) -> bool:
        if df is None or df.empty or len(df) < min(days, 20):
            return False
        if "date" not in df.columns:
            return False
        latest = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(latest):
            return False
        latest_date = latest.date() if isinstance(latest, datetime) else latest.to_pydatetime().date()
        return date.today() - latest_date <= timedelta(days=self.stale_days)


def _code_variants(code: str) -> Tuple[str, str]:
    suffix = ".SH" if code.startswith("6") else ".SZ"
    return code, f"{code}{suffix}"
