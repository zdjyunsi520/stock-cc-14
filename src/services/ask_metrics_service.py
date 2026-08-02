# -*- coding: utf-8 -*-
"""Deterministic metric context for /ask stock questions."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager, get_data_fetcher_manager, normalize_stock_code
from data_provider.realtime_types import safe_float
from src.stock_analyzer import StockTrendAnalyzer

logger = logging.getLogger(__name__)


DataQuality = Dict[str, str]


def build_ask_metrics_context(stock_code: str, config=None) -> Dict[str, Any]:
    """Build a fail-open deterministic context block for Claude stock Q&A."""
    service = AskMetricsService(config=config)
    return service.build(stock_code)


class AskMetricsService:
    """Aggregate existing read-mostly data capabilities for stock questions."""

    def __init__(
        self,
        *,
        config=None,
        data_manager: Optional[DataFetcherManager] = None,
        minute_provider: Optional[Callable[[str], Sequence[Dict[str, Any]]]] = None,
        trend_analyzer: Optional[StockTrendAnalyzer] = None,
    ) -> None:
        self.config = config
        self.data_manager = data_manager or get_data_fetcher_manager()
        self.minute_provider = minute_provider
        self.trend_analyzer = trend_analyzer or StockTrendAnalyzer()

    def build(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(str(stock_code or "")) or str(stock_code or "").strip().upper()
        context: Dict[str, Any] = {"code": code, "data_quality": {}}
        data_quality: DataQuality = context["data_quality"]

        quote = self._safe_collect("realtime_quote", data_quality, lambda: self.data_manager.get_realtime_quote(code, log_final_failure=False))
        if quote:
            context["realtime_quote"] = _quote_to_dict(quote)

        daily_payload = self._safe_collect("daily_data", data_quality, lambda: self.data_manager.get_daily_data(code, days=80))
        daily_df = pd.DataFrame()
        daily_source = ""
        if isinstance(daily_payload, tuple) and len(daily_payload) >= 1:
            daily_df = daily_payload[0] if isinstance(daily_payload[0], pd.DataFrame) else pd.DataFrame(daily_payload[0])
            daily_source = str(daily_payload[1] if len(daily_payload) > 1 else "")
        if not daily_df.empty:
            context["daily_source"] = daily_source
            context["twenty_day"] = calculate_twenty_day_metrics(daily_df)
            if context["twenty_day"].get("status") != "ok":
                data_quality["twenty_day"] = str(context["twenty_day"].get("status"))
            else:
                data_quality["twenty_day"] = "ok"
            context["trend"] = self._build_trend_context(code, daily_df, data_quality)
        else:
            data_quality["twenty_day"] = "insufficient_data"
            data_quality["trend"] = "insufficient_data"

        fundamental = self._safe_collect("fundamental", data_quality, lambda: self.data_manager.get_fundamental_context(code, budget_seconds=2.0))
        if isinstance(fundamental, dict) and fundamental:
            context["fundamental"] = _compact_fundamental_context(fundamental)

        market = self._build_market_context(data_quality)
        if market:
            context["market"] = market

        hot_themes = self._build_hot_theme_context(code, data_quality)
        if hot_themes:
            context["hot_themes"] = hot_themes

        minute = self._build_minute_context(code, data_quality)
        if minute:
            context["intraday"] = minute

        return context

    def _safe_collect(self, key: str, data_quality: DataQuality, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception as exc:
            logger.warning("[AskMetrics] %s failed: %s", key, exc)
            data_quality[key] = "failed"
            return None
        if value is None or value == [] or value == {}:
            data_quality[key] = "unavailable"
            return value
        data_quality[key] = "ok"
        return value

    def _build_trend_context(self, code: str, daily_df: pd.DataFrame, data_quality: DataQuality) -> Dict[str, Any]:
        try:
            result = self.trend_analyzer.analyze(daily_df, code)
            data_quality["trend"] = "ok" if len(daily_df) >= 20 else "insufficient_data"
            return result.to_dict()
        except Exception as exc:
            logger.warning("[AskMetrics] trend failed: %s", exc, exc_info=True)
            data_quality["trend"] = "failed"
            return {}

    def _build_market_context(self, data_quality: DataQuality) -> Dict[str, Any]:
        indices = self._safe_collect("main_indices", data_quality, lambda: self.data_manager.get_main_indices())
        stats = self._safe_collect("market_stats", data_quality, lambda: self.data_manager.get_market_stats())
        payload = {
            "bias": classify_market_bias(indices if isinstance(indices, list) else [], stats if isinstance(stats, dict) else {}),
            "indices": _compact_indices(indices if isinstance(indices, list) else []),
            "stats": stats if isinstance(stats, dict) else {},
        }
        return {k: v for k, v in payload.items() if v not in ({}, [])}

    def _build_hot_theme_context(self, code: str, data_quality: DataQuality) -> Dict[str, Any]:
        concept_top, _ = self._safe_collect("concept_rankings", data_quality, lambda: self.data_manager.get_concept_rankings(5)) or ([], [])
        sector_top, _ = self._safe_collect("sector_rankings", data_quality, lambda: self.data_manager.get_sector_rankings(5)) or ([], [])
        universe = self._safe_collect("hot_theme_universe", data_quality, lambda: self.data_manager.get_hot_theme_universe(n=5, max_members_per_theme=80))
        matched = []
        if isinstance(universe, dict):
            matched = list(universe.get(code, []) or [])
        return {
            "matched_themes": matched,
            "top_concepts": _compact_boards(concept_top),
            "top_sectors": _compact_boards(sector_top),
        }

    def _build_minute_context(self, code: str, data_quality: DataQuality) -> Dict[str, Any]:
        provider = self.minute_provider
        if provider is None:
            try:
                from src.services.intraday_minute_provider import IntradayMinuteCacheProvider

                provider = IntradayMinuteCacheProvider()
            except Exception as exc:
                logger.warning("[AskMetrics] minute provider unavailable: %s", exc, exc_info=True)
                data_quality["intraday"] = "unavailable"
                return {}
        try:
            rows = list(provider(code) or [])
        except Exception as exc:
            logger.warning("[AskMetrics] intraday failed: %s", exc, exc_info=True)
            data_quality["intraday"] = "failed"
            return {}
        metrics = calculate_minute_metrics(rows)
        data_quality["intraday"] = str(metrics.get("status") or "unknown")
        return metrics


def calculate_twenty_day_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """Calculate recent 20-trading-day range metrics from daily bars."""
    if df is None or df.empty or len(df) < 21:
        return {"status": "insufficient_data", "rows": 0 if df is None else len(df)}

    data = df.copy()
    if "date" in data.columns:
        data = data.sort_values("date")
    data = data.tail(21).reset_index(drop=True)
    close = pd.to_numeric(data.get("close"), errors="coerce")
    high = pd.to_numeric(data.get("high", close), errors="coerce")
    low = pd.to_numeric(data.get("low", close), errors="coerce")
    pct_chg = pd.to_numeric(data.get("pct_chg"), errors="coerce") if "pct_chg" in data.columns else None
    if close.isna().all() or len(close.dropna()) < 21:
        return {"status": "insufficient_data", "rows": len(data)}

    start_close = safe_float(close.iloc[0])
    end_close = safe_float(close.iloc[-1])
    high_20 = safe_float(high.tail(20).max())
    low_20 = safe_float(low.tail(20).min())
    max_drawdown = _max_drawdown_pct(close.tail(20).dropna().tolist())
    limit_up_count = 0
    if pct_chg is not None:
        limit_up_count = int((pct_chg.tail(20) >= 9.5).sum())
    elif len(close.dropna()) >= 2:
        changes = close.pct_change() * 100
        limit_up_count = int((changes.tail(20) >= 9.5).sum())

    return {
        "status": "ok",
        "rows": len(data),
        "twenty_day_return_pct": _pct(end_close, start_close),
        "dist_20d_high_pct": _pct(end_close, high_20),
        "dist_20d_low_pct": _pct(end_close, low_20),
        "limit_up_count_20d": limit_up_count,
        "max_drawdown_20d_pct": max_drawdown,
    }


def calculate_minute_metrics(rows: Sequence[Dict[str, Any]], tail_minutes: int = 30) -> Dict[str, Any]:
    points: List[Tuple[float, float]] = []
    for row in rows or []:
        price = safe_float(row.get("price") or row.get("close"))
        avg_price = safe_float(row.get("avg_price") or row.get("average_price"))
        if price is not None and avg_price is not None:
            points.append((price, avg_price))
    if len(points) < 10:
        return {"status": "insufficient_data", "rows": len(points)}

    prices = [item[0] for item in points]
    above_avg_ratio = sum(1 for price, avg in points if price >= avg) / len(points)
    max_price = max(prices)
    high_index = prices.index(max_price)
    after_high = points[high_index + 1 :]
    break_count = sum(1 for price, avg in after_high if price < avg)
    tail_len = min(max(1, int(tail_minutes)), len(points))
    tail = prices[-tail_len:]
    tail_gain_pct = _pct(tail[-1], tail[0]) if len(tail) >= 2 else 0.0
    tail_high = max(tail)
    tail_close_near_high_pct = _pct(tail[-1], tail_high)
    reversal_count = sum(1 for prev, curr in zip(tail, tail[1:]) if curr < prev)

    return {
        "status": "checked",
        "rows": len(points),
        "above_avg_ratio": round(above_avg_ratio, 4),
        "pullback_break_count": break_count,
        "pullback_holds": break_count <= 3,
        "tail_gain_pct": tail_gain_pct,
        "tail_reversal_count": reversal_count,
        "tail_close_near_high_pct": tail_close_near_high_pct,
        "tail_supports": tail_gain_pct >= 0 and tail_close_near_high_pct >= -0.8 and reversal_count <= max(8, tail_len // 3),
    }


def classify_market_bias(indices: Sequence[Dict[str, Any]], stats: Dict[str, Any]) -> str:
    up_count = _first_number(stats, "up_count", "rise_count", "上涨家数")
    down_count = _first_number(stats, "down_count", "fall_count", "下跌家数")
    if up_count is not None and down_count is not None and up_count + down_count > 0:
        ratio = up_count / (up_count + down_count)
        if ratio >= 0.6:
            return "risk_on"
        if ratio <= 0.4:
            return "risk_off"
        return "mixed"

    changes = []
    for item in indices or []:
        change = _first_number(item, "change_pct", "pct_chg", "涨跌幅")
        if change is not None:
            changes.append(change)
    if not changes:
        return "unknown"
    avg_change = sum(changes) / len(changes)
    if avg_change >= 0.3:
        return "risk_on"
    if avg_change <= -0.3:
        return "risk_off"
    return "mixed"


def _quote_to_dict(quote: Any) -> Dict[str, Any]:
    if isinstance(quote, dict):
        source = quote
    elif hasattr(quote, "to_dict"):
        source = quote.to_dict()
    else:
        fields = (
            "code", "name", "price", "change_pct", "volume_ratio", "turnover_rate",
            "amount", "pe_ratio", "pb_ratio", "circ_mv", "total_mv", "source",
            "provider_timestamp", "is_stale", "stale_seconds",
        )
        source = {field: getattr(quote, field, None) for field in fields}
    compact = {key: value for key, value in source.items() if value is not None}
    for key in ("circ_mv", "total_mv"):
        if key in compact:
            compact[f"{key}_yi"] = round(float(compact[key]) / 100000000, 2)
    return compact


def _compact_fundamental_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("market", "valuation", "growth", "earnings", "institution", "capital_flow", "dragon_tiger", "boards", "coverage", "errors")
    return {key: ctx.get(key) for key in keys if ctx.get(key) not in (None, {}, [])}


def _compact_indices(indices: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_compact_mapping(item, ("code", "name", "price", "change_pct", "amount")) for item in list(indices or [])[:5]]


def _compact_boards(boards: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_compact_mapping(item, ("name", "change_pct", "volume_ratio", "amount")) for item in list(boards or [])[:5]]


def _compact_mapping(item: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    return {key: item.get(key) for key in keys if isinstance(item, dict) and item.get(key) is not None}


def _first_number(mapping: Dict[str, Any], *keys: str) -> Optional[float]:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = safe_float(mapping.get(key))
        if value is not None:
            return value
    return None


def _pct(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base in (None, 0):
        return None
    return round((float(current) - float(base)) / float(base) * 100, 2)


def _max_drawdown_pct(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    peak = float(values[0])
    max_drawdown = 0.0
    for value in values:
        current = float(value)
        peak = max(peak, current)
        if peak > 0:
            max_drawdown = min(max_drawdown, (current - peak) / peak * 100)
    return round(max_drawdown, 2)
