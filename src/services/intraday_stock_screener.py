# -*- coding: utf-8 -*-
"""Intraday stock screener for main-board momentum candidates."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from data_provider.base import is_bse_code, is_kc_cy_stock, is_st_stock, normalize_stock_code

logger = logging.getLogger(__name__)


@dataclass
class IntradayScreenerCriteria:
    change_pct_min: float = 3.0
    change_pct_max: float = 5.0
    history_days: int = 40
    limit_up_lookback_days: int = 20
    limit_up_threshold_pct: float = 9.8
    min_volume_ratio: float = 1.0
    turnover_rate_min: float = 0.7
    turnover_rate_max: Optional[float] = None
    circ_mv_min: float = 5_000_000_000.0
    circ_mv_max: float = 20_000_000_000.0
    capital_flow_abs_ratio_max: float = 0.12
    intraday_above_avg_min_ratio: float = 0.95
    intraday_avg_tolerance_pct: float = 0.2
    tail_minutes: int = 30
    tail_gain_min_pct: float = 0.3
    require_intraday_pattern: bool = False
    max_candidates_for_deep_check: int = 80


@dataclass
class IntradayPatternResult:
    status: str
    passed: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IntradayScreenCandidate:
    code: str
    name: str
    price: Optional[float]
    change_pct: Optional[float]
    volume_ratio: Optional[float]
    turnover_rate: Optional[float]
    circ_mv: Optional[float]
    amount: Optional[float]
    passed: bool
    rejected_reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IntradayScreenResult:
    passed: List[IntradayScreenCandidate]
    rejected: List[IntradayScreenCandidate]
    checked_count: int
    rough_count: int
    criteria: IntradayScreenerCriteria
    data_quality: Dict[str, Any] = field(default_factory=dict)


class IntradayStockScreener:
    """筛选主板盘中动量候选股。"""

    def __init__(
        self,
        *,
        snapshot_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        daily_provider: Optional[Callable[[str, int], Tuple[pd.DataFrame, str]]] = None,
        capital_flow_provider: Optional[Callable[[str], Dict[str, Any]]] = None,
        minute_provider: Optional[Callable[[str], Optional[Sequence[Dict[str, Any]]]]] = None,
    ) -> None:
        self.snapshot_provider = snapshot_provider or self._default_snapshot_provider
        self.daily_provider = daily_provider or self._default_daily_provider
        self.capital_flow_provider = capital_flow_provider or self._default_capital_flow_provider
        self.minute_provider = minute_provider

    @staticmethod
    def _default_manager():
        from data_provider.base import DataFetcherManager

        return DataFetcherManager()

    def _default_snapshot_provider(self) -> List[Dict[str, Any]]:
        return self._default_manager().get_a_share_realtime_snapshot()

    def _default_daily_provider(self, code: str, days: int) -> Tuple[pd.DataFrame, str]:
        return self._default_manager().get_daily_data(code, days=days)

    def _default_capital_flow_provider(self, code: str) -> Dict[str, Any]:
        return self._default_manager().get_capital_flow_context(code)

    def screen(self, criteria: Optional[IntradayScreenerCriteria] = None) -> IntradayScreenResult:
        criteria = criteria or IntradayScreenerCriteria()
        snapshot = self.snapshot_provider() or []
        rough_rows = self._rough_filter(snapshot, criteria)
        limited_rows = rough_rows[: max(0, int(criteria.max_candidates_for_deep_check))]

        passed: List[IntradayScreenCandidate] = []
        rejected: List[IntradayScreenCandidate] = []
        for row in limited_rows:
            candidate = self._deep_check(row, criteria)
            if candidate.passed:
                passed.append(candidate)
            else:
                rejected.append(candidate)

        passed.sort(key=_candidate_reliability_sort_key, reverse=True)
        rejected.sort(key=_candidate_reliability_sort_key, reverse=True)

        skipped = max(0, len(rough_rows) - len(limited_rows))
        return IntradayScreenResult(
            passed=passed,
            rejected=rejected,
            checked_count=len(limited_rows),
            rough_count=len(rough_rows),
            criteria=criteria,
            data_quality={
                "snapshot_count": len(snapshot),
                "rough_count": len(rough_rows),
                "deep_check_skipped": skipped,
                "minute_pattern_required": criteria.require_intraday_pattern,
                "minute_provider_available": self.minute_provider is not None,
            },
        )

    def _rough_filter(
        self,
        snapshot: Iterable[Dict[str, Any]],
        criteria: IntradayScreenerCriteria,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw in snapshot:
            code = normalize_stock_code(str(raw.get("code") or ""))
            name = str(raw.get("name") or "").strip()
            if not code or not self._is_main_board_stock(code, name):
                continue

            change_pct = _safe_float(raw.get("change_pct"))
            volume_ratio = _safe_float(raw.get("volume_ratio"))
            turnover_rate = _safe_float(raw.get("turnover_rate"))
            circ_mv = _safe_float(raw.get("circ_mv"))
            if change_pct is None or not (criteria.change_pct_min <= change_pct <= criteria.change_pct_max):
                continue
            if volume_ratio is None or volume_ratio <= criteria.min_volume_ratio:
                continue
            if turnover_rate is None or turnover_rate <= criteria.turnover_rate_min:
                continue
            if criteria.turnover_rate_max is not None and turnover_rate > criteria.turnover_rate_max:
                continue
            if circ_mv is None or not (criteria.circ_mv_min <= circ_mv <= criteria.circ_mv_max):
                continue
            rows.append({**raw, "code": code, "name": name})
        rows.sort(key=lambda item: (_safe_float(item.get("change_pct")) or 0), reverse=True)
        return rows

    def _deep_check(self, row: Dict[str, Any], criteria: IntradayScreenerCriteria) -> IntradayScreenCandidate:
        code = normalize_stock_code(str(row.get("code") or ""))
        rejected: List[str] = []
        metrics: Dict[str, Any] = {}
        data_quality: Dict[str, Any] = {}

        try:
            history, source = self.daily_provider(code, criteria.history_days)
            data_quality["daily_source"] = source
        except Exception as exc:
            history = pd.DataFrame()
            data_quality["daily_error"] = str(exc)
            rejected.append("daily_history_unavailable")

        if history.empty:
            rejected.append("daily_history_unavailable")
        else:
            ma_passed, ma_metrics = self._check_ma_upward(history)
            metrics.update(ma_metrics)
            if not ma_passed:
                rejected.append("ma5_ma10_ma20_not_all_upward")
            limit_up_metrics = self._limit_up_memory_metrics(history, criteria)
            metrics.update(limit_up_metrics)
            if not limit_up_metrics["has_recent_limit_up"]:
                rejected.append("no_limit_up_in_20_days")
            metrics.update(_daily_shape_metrics(history))

        capital_passed, capital_metrics, capital_quality = self._check_capital_flow(row, criteria)
        metrics.update(capital_metrics)
        data_quality.update(capital_quality)
        if not capital_passed:
            rejected.append("capital_flow_abnormal")

        pattern = self._check_intraday_pattern(code, criteria)
        metrics["intraday_pattern"] = asdict(pattern)
        if criteria.require_intraday_pattern and not pattern.passed:
            rejected.append("intraday_pattern_not_confirmed")
        elif pattern.status == "not_checked":
            data_quality["intraday_pattern"] = "not_checked"

        reliability = _score_candidate_reliability(row, metrics, rejected)
        metrics["reliability_score"] = reliability["score"]
        metrics["reliability_reasons"] = reliability["reasons"]

        return IntradayScreenCandidate(
            code=code,
            name=str(row.get("name") or ""),
            price=_safe_float(row.get("price")),
            change_pct=_safe_float(row.get("change_pct")),
            volume_ratio=_safe_float(row.get("volume_ratio")),
            turnover_rate=_safe_float(row.get("turnover_rate")),
            circ_mv=_safe_float(row.get("circ_mv")),
            amount=_safe_float(row.get("amount")),
            passed=not rejected,
            rejected_reasons=rejected,
            metrics=metrics,
            data_quality=data_quality,
        )

    def _check_capital_flow(
        self,
        row: Dict[str, Any],
        criteria: IntradayScreenerCriteria,
    ) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
        code = str(row.get("code") or "")
        amount = _safe_float(row.get("amount"))
        try:
            ctx = self.capital_flow_provider(code)
        except Exception as exc:
            return True, {}, {"capital_flow_status": "error", "capital_flow_error": str(exc)}

        status = str(ctx.get("status", "unknown"))
        data = ctx.get("data") or {}
        stock_flow = data.get("stock_flow") if isinstance(data, dict) else {}
        if not stock_flow and "main_net_inflow" in ctx:
            stock_flow = ctx
        main_net_inflow = _safe_float((stock_flow or {}).get("main_net_inflow"))
        metrics = {"capital_flow_status": status, "main_net_inflow": main_net_inflow}
        quality = {"capital_flow_status": status}
        if main_net_inflow is None or amount is None or amount <= 0:
            quality["capital_flow_check"] = "insufficient_data"
            return True, metrics, quality

        ratio = main_net_inflow / amount
        metrics["main_net_inflow_amount_ratio"] = ratio
        return abs(ratio) <= criteria.capital_flow_abs_ratio_max, metrics, quality

    def _check_intraday_pattern(
        self,
        code: str,
        criteria: IntradayScreenerCriteria,
    ) -> IntradayPatternResult:
        if self.minute_provider is None:
            return IntradayPatternResult(
                status="not_checked",
                passed=not criteria.require_intraday_pattern,
                reasons=["minute_provider_unavailable"],
            )
        rows = list(self.minute_provider(code) or [])
        points = [_minute_point(row) for row in rows]
        points = [point for point in points if point[0] is not None and point[1] is not None]
        if len(points) < 10:
            return IntradayPatternResult(status="insufficient_data", passed=False, reasons=["minute_rows_insufficient"])

        tolerance = max(0.0, criteria.intraday_avg_tolerance_pct) / 100.0
        above_count = sum(1 for price, avg in points if price >= avg * (1 - tolerance))
        above_ratio = above_count / len(points)
        prices = [point[0] for point in points]
        avgs = [point[1] for point in points]
        high_index = max(range(len(prices)), key=lambda idx: prices[idx])
        after_high = list(zip(prices[high_index:], avgs[high_index:]))
        pullback_breaks = [
            (avg * (1 - tolerance) - price, avg)
            for price, avg in after_high
            if avg and price < avg * (1 - tolerance)
        ]
        pullback_break_count = len(pullback_breaks)
        pullback_max_break_pct = max((diff / avg) * 100 for diff, avg in pullback_breaks) if pullback_breaks else 0.0
        pullback_break_ratio = pullback_break_count / len(after_high) if after_high else 0.0
        pullback_holds = bool(after_high) and pullback_break_count == 0
        tail_len = min(max(3, int(criteria.tail_minutes)), len(prices))
        tail_prices = prices[-tail_len:]
        tail_gain = (tail_prices[-1] / tail_prices[0] - 1) * 100 if tail_prices[0] else 0.0
        tail_high = max(tail_prices)
        tail_drawdown = (min(tail_prices) / tail_high - 1) * 100 if tail_high else 0.0
        tail_close_near_high_pct = (tail_prices[-1] / tail_high - 1) * 100 if tail_high else 0.0
        tail_down_tick_count = sum(1 for prev, curr in zip(tail_prices, tail_prices[1:]) if curr < prev)
        tail_directions = [
            1 if curr > prev else -1 if curr < prev else 0
            for prev, curr in zip(tail_prices, tail_prices[1:])
        ]
        tail_directions = [direction for direction in tail_directions if direction]
        tail_reversal_count = sum(
            1
            for prev, curr in zip(tail_directions, tail_directions[1:])
            if prev != curr
        )
        tail_lifts = (
            tail_gain >= criteria.tail_gain_min_pct
            and tail_close_near_high_pct >= -0.3
            and tail_reversal_count <= max(2, tail_len // 6)
        )

        above_avg_passed = above_ratio >= criteria.intraday_above_avg_min_ratio
        reasons: List[str] = []
        if not above_avg_passed:
            reasons.append("price_not_above_intraday_average_enough")
        if not pullback_holds:
            reasons.append("pullback_broke_intraday_average")
        if not tail_lifts:
            reasons.append("tail_not_steadily_lifting")

        return IntradayPatternResult(
            status="checked",
            passed=not reasons,
            reasons=reasons,
            metrics={
                "above_avg_ratio": above_ratio,
                "above_avg_passed": above_avg_passed,
                "pullback_holds": pullback_holds,
                "pullback_break_count": pullback_break_count,
                "pullback_break_ratio": pullback_break_ratio,
                "pullback_max_break_pct": pullback_max_break_pct,
                "tail_lifts": tail_lifts,
                "tail_down_tick_count": tail_down_tick_count,
                "tail_reversal_count": tail_reversal_count,
                "tail_close_near_high_pct": tail_close_near_high_pct,
                "intraday_high_index": high_index,
                "tail_gain_pct": tail_gain,
                "tail_drawdown_pct": tail_drawdown,
            },
        )

    @staticmethod
    def _check_ma_upward(history: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        df = history.copy().sort_values("date") if "date" in history.columns else history.copy()
        for window in (5, 10, 20):
            col = f"ma{window}"
            if col not in df.columns:
                df[col] = pd.to_numeric(df["close"], errors="coerce").rolling(window=window).mean()
        if len(df) < 21:
            return False, {"ma_check": "insufficient_history"}
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        metrics = {
            "ma5": _safe_float(latest.get("ma5")),
            "ma10": _safe_float(latest.get("ma10")),
            "ma20": _safe_float(latest.get("ma20")),
            "ma5_prev": _safe_float(prev.get("ma5")),
            "ma10_prev": _safe_float(prev.get("ma10")),
            "ma20_prev": _safe_float(prev.get("ma20")),
        }
        passed = all(
            metrics[f"ma{window}"] is not None
            and metrics[f"ma{window}_prev"] is not None
            and metrics[f"ma{window}"] > metrics[f"ma{window}_prev"]
            for window in (5, 10, 20)
        )
        return passed, metrics

    @staticmethod
    def _limit_up_memory_metrics(history: pd.DataFrame, criteria: IntradayScreenerCriteria) -> Dict[str, Any]:
        if "pct_chg" not in history.columns:
            return {
                "has_recent_limit_up": False,
                "limit_up_count_20d": 0,
                "recent_limit_up_count_5d": 0,
            }
        pct = pd.to_numeric(history["pct_chg"], errors="coerce").tail(criteria.limit_up_lookback_days)
        flags = pct >= criteria.limit_up_threshold_pct
        limit_up_count = int(flags.sum())
        recent_limit_up_count = int(flags.tail(5).sum())
        return {
            "has_recent_limit_up": limit_up_count > 0,
            "limit_up_count_20d": limit_up_count,
            "recent_limit_up_count_5d": recent_limit_up_count,
        }

    @staticmethod
    def _is_main_board_stock(code: str, name: str) -> bool:
        return len(code) == 6 and not is_bse_code(code) and not is_kc_cy_stock(code) and not is_st_stock(name)


def _daily_shape_metrics(history: pd.DataFrame) -> Dict[str, Any]:
    if history.empty:
        return {}
    df = history.copy().sort_values("date") if "date" in history.columns else history.copy()
    latest = df.iloc[-1]
    open_price = _safe_float(latest.get("open"))
    high = _safe_float(latest.get("high"))
    low = _safe_float(latest.get("low"))
    close = _safe_float(latest.get("close"))
    metrics: Dict[str, Any] = {}
    if open_price and close:
        metrics["daily_body_pct"] = (close / open_price - 1) * 100
    if high and close and open_price:
        metrics["upper_shadow_pct"] = (high - max(open_price, close)) / close * 100 if close else None
    if low and close and open_price:
        metrics["lower_shadow_pct"] = (min(open_price, close) - low) / close * 100 if close else None
    closes = pd.to_numeric(df["close"], errors="coerce").tail(20) if "close" in df.columns else pd.Series(dtype=float)
    high_20 = closes.max() if not closes.empty else None
    if close and high_20 and high_20 > 0:
        metrics["drawdown_from_20d_high_pct"] = (close / high_20 - 1) * 100
    return metrics


def _score_candidate_reliability(
    row: Dict[str, Any],
    metrics: Dict[str, Any],
    rejected: Sequence[str],
) -> Dict[str, Any]:
    score = 0.0
    reasons: List[str] = []

    pattern = metrics.get("intraday_pattern") or {}
    pattern_metrics = pattern.get("metrics") if isinstance(pattern, dict) else {}
    above_ratio = _safe_float((pattern_metrics or {}).get("above_avg_ratio"))
    tail_gain = _safe_float((pattern_metrics or {}).get("tail_gain_pct"))
    tail_drawdown = _safe_float((pattern_metrics or {}).get("tail_drawdown_pct"))
    above_avg_passed = bool((pattern_metrics or {}).get("above_avg_passed"))
    pullback_holds = bool((pattern_metrics or {}).get("pullback_holds"))
    pullback_break_count = int(_safe_float((pattern_metrics or {}).get("pullback_break_count")) or 0)
    pullback_break_ratio = _safe_float((pattern_metrics or {}).get("pullback_break_ratio")) or 0.0
    pullback_max_break_pct = _safe_float((pattern_metrics or {}).get("pullback_max_break_pct")) or 0.0
    tail_lifts = bool((pattern_metrics or {}).get("tail_lifts"))
    tail_reversal_count = int(_safe_float((pattern_metrics or {}).get("tail_reversal_count")) or 0)
    tail_close_near_high_pct = _safe_float((pattern_metrics or {}).get("tail_close_near_high_pct")) or 0.0

    if above_ratio is not None:
        add = min(18.0, max(0.0, above_ratio) * 18.0)
        score += add
        reasons.append(f"分时均线上方占比 {above_ratio:.2f} +{add:.1f}")
    if above_avg_passed:
        score += 6.0
        reasons.append("全天站上分时均线达标 +6")

    if pattern.get("status") == "checked":
        pullback_score = max(0.0, 10.0 - pullback_break_count * 1.5 - pullback_break_ratio * 10.0 - pullback_max_break_pct * 2.0)
        score += pullback_score
        if pullback_holds:
            reasons.append(f"创新高后回踩均线不破 +{pullback_score:.1f}")
        else:
            reasons.append(
                f"回踩破均线 {pullback_break_count} 次/最深 {pullback_max_break_pct:.2f}% +{pullback_score:.1f}"
            )

        tail_score = max(0.0, min(10.0, (tail_gain or 0.0) * 2.0 + 5.0 - tail_reversal_count * 1.2 + tail_close_near_high_pct))
        score += tail_score
        if tail_lifts:
            reasons.append(f"尾盘稳步拉升 +{tail_score:.1f}")
        else:
            reasons.append(
                f"尾盘涨幅 {tail_gain or 0.0:.2f}%/往复 {tail_reversal_count} 次/离高点 {tail_close_near_high_pct:.2f}% +{tail_score:.1f}"
            )

    if pattern.get("passed"):
        score += 12.0
        reasons.append("分时三项全部满足 +12")

    limit_up_count = int(_safe_float(metrics.get("limit_up_count_20d")) or 0)
    recent_limit_up_count = int(_safe_float(metrics.get("recent_limit_up_count_5d")) or 0)
    if limit_up_count > 0:
        memory_score = 10.0 if limit_up_count == 1 else 13.0 if limit_up_count == 2 else 15.0
        recent_score = min(2.0, recent_limit_up_count * 1.0)
        add = min(15.0, memory_score + recent_score)
        score += add
        reasons.append(f"20日涨停记忆 {limit_up_count} 次/近5日 {recent_limit_up_count} 次 +{add:.1f}")
    if "ma5_ma10_ma20_not_all_upward" not in rejected:
        score += 15.0
        reasons.append("5/10/20日均线同步向上 +15")

    body_pct = _safe_float(metrics.get("daily_body_pct"))
    upper_shadow = _safe_float(metrics.get("upper_shadow_pct"))
    drawdown_20 = _safe_float(metrics.get("drawdown_from_20d_high_pct"))
    if body_pct is not None and body_pct > 2.0:
        add = min(8.0, body_pct)
        score += add
        reasons.append(f"日K实体强 {body_pct:.1f}% +{add:.1f}")
    if upper_shadow is not None and upper_shadow > 3.0:
        penalty = min(8.0, upper_shadow)
        score -= penalty
        reasons.append(f"上影线偏长 -{penalty:.1f}")
    if drawdown_20 is not None and drawdown_20 >= -10.0:
        score += 5.0
        reasons.append("距离20日高点不远 +5")

    volume_ratio = _safe_float(row.get("volume_ratio"))
    turnover_rate = _safe_float(row.get("turnover_rate"))
    if volume_ratio is not None and 1.0 < volume_ratio <= 3.0:
        score += 4.0
        reasons.append("量比温和放大 +4")
    elif volume_ratio is not None and volume_ratio > 4.0:
        score -= 3.0
        reasons.append("量比过高需防兑现 -3")
    if turnover_rate is not None:
        if turnover_rate >= 25.0:
            score -= 3.0
            reasons.append(f"换手 {turnover_rate:.1f}% 极高，分歧偏大 -3")
        elif turnover_rate >= 15.0:
            score -= 1.0
            reasons.append(f"换手 {turnover_rate:.1f}% 偏高，注意分歧 -1")
        elif turnover_rate >= 2.0:
            reasons.append(f"换手 {turnover_rate:.1f}% 活跃度达标")
        else:
            reasons.append(f"换手 {turnover_rate:.1f}% 低位活跃，仅作筛选通过")

    if tail_gain is not None and tail_gain > 0:
        add = min(5.0, tail_gain)
        score += add
        reasons.append(f"尾盘仍有抬升 +{add:.1f}")
    if tail_drawdown is not None and tail_drawdown < -3.0:
        penalty = min(5.0, abs(tail_drawdown) - 3.0)
        score -= penalty
        reasons.append(f"尾盘回撤偏大 -{penalty:.1f}")

    score = max(0.0, min(100.0, score))
    return {"score": round(score, 2), "reasons": reasons}


def _candidate_reliability_sort_key(candidate: IntradayScreenCandidate) -> Tuple[int, float, float]:
    pattern = candidate.metrics.get("intraday_pattern") if isinstance(candidate.metrics, dict) else {}
    pattern_complete = 1 if isinstance(pattern, dict) and pattern.get("passed") else 0
    return (
        pattern_complete,
        _safe_float(candidate.metrics.get("reliability_score")) or 0.0,
        candidate.change_pct or 0.0,
    )


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text or text in {"-", "--", "None", "nan"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _minute_point(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    price = _safe_float(row.get("price") or row.get("close") or row.get("current"))
    avg = _safe_float(row.get("avg_price") or row.get("average_price") or row.get("mean_price") or row.get("均价"))
    return price, avg


def screen_intraday_momentum_stocks(criteria: Optional[IntradayScreenerCriteria] = None) -> IntradayScreenResult:
    criteria = criteria or IntradayScreenerCriteria()
    daily_provider = None
    minute_provider = None
    from src.config import get_config

    if get_config().tushare_token:
        from src.services.daily_history_cache_provider import DailyHistoryCacheProvider

        daily_provider = DailyHistoryCacheProvider()
    if criteria.require_intraday_pattern:
        from src.services.intraday_minute_provider import IntradayMinuteCacheProvider

        minute_provider = IntradayMinuteCacheProvider()
    return IntradayStockScreener(daily_provider=daily_provider, minute_provider=minute_provider).screen(criteria)


def _candidate_to_dict(candidate: IntradayScreenCandidate) -> Dict[str, Any]:
    data = asdict(candidate)
    data["circ_mv_yi"] = round(candidate.circ_mv / 100000000, 2) if candidate.circ_mv is not None else None
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="盘中主板动量选股")
    parser.add_argument("--require-intraday-pattern", action="store_true", help="要求分时形态复核通过")
    parser.add_argument("--limit", type=int, default=20, help="输出候选数量")
    args = parser.parse_args()

    criteria = IntradayScreenerCriteria(require_intraday_pattern=args.require_intraday_pattern)
    result = screen_intraday_momentum_stocks(criteria)
    ranked = sorted([*result.passed, *result.rejected], key=_candidate_reliability_sort_key, reverse=True)
    payload = {
        "passed": [_candidate_to_dict(item) for item in result.passed[: args.limit]],
        "ranked_candidates": [_candidate_to_dict(item) for item in ranked[: args.limit]],
        "rejected_count": len(result.rejected),
        "checked_count": result.checked_count,
        "rough_count": result.rough_count,
        "data_quality": result.data_quality,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
