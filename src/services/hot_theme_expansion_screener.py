# -*- coding: utf-8 -*-
"""Hot-theme expansion screener for next-session watchlists."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from data_provider.base import normalize_stock_code


ThemeUniverse = Dict[str, Sequence[str]]


DEFAULT_THEME_UNIVERSE: ThemeUniverse = {
    "002837": ("液冷",),
    "300602": ("液冷",),
    "300499": ("液冷",),
    "301018": ("液冷",),
    "300249": ("液冷",),
    "300731": ("液冷",),
    "002463": ("PCB",),
    "600183": ("CCL", "覆铜板"),
    "603228": ("PCB",),
    "002916": ("PCB",),
    "300476": ("PCB",),
    "002636": ("CCL", "覆铜板"),
    "603186": ("CCL", "覆铜板"),
    "688519": ("CCL", "覆铜板"),
    "600601": ("PCB",),
    "002815": ("PCB",),
    "002579": ("PCB",),
    "002913": ("PCB",),
    "002134": ("PCB",),
    "603002": ("CCL",),
    "301511": ("铜箔",),
    "688388": ("铜箔",),
    "600110": ("铜箔",),
    "301217": ("铜箔",),
    "301150": ("铜箔",),
    "002992": ("PET铜箔",),
    "300057": ("PET铜箔",),
    "002741": ("铜箔",),
    "000823": ("PCB",),
    "002436": ("PCB",),
    "603920": ("PCB",),
    "605258": ("PCB",),
    "002938": ("PCB",),
    "603386": ("PCB",),
    "002484": ("PCB",),
}


@dataclass
class HotThemeExpansionCriteria:
    history_days: int = 60
    max_candidates: int = 20
    min_theme_score: float = 25.0
    laggard_change_min_pct: float = -2.0
    laggard_change_max_pct: float = 5.5
    overheat_change_pct: float = 8.0
    active_volume_ratio_min: float = 1.0
    active_turnover_min: float = 1.0
    active_turnover_max: float = 15.0
    minute_above_avg_min_ratio: float = 0.8
    tail_minutes: int = 30


@dataclass
class ThemeExpansionState:
    theme: str
    score: float
    member_count: int
    active_count: int
    up_count: int
    avg_change_pct: Optional[float]
    avg_volume_ratio: Optional[float]
    total_amount: float
    stage: str
    reasons: List[str] = field(default_factory=list)


@dataclass
class HotThemeExpansionCandidate:
    code: str
    name: str
    themes: List[str]
    score: float
    theme_score: float
    price: Optional[float]
    change_pct: Optional[float]
    volume_ratio: Optional[float]
    turnover_rate: Optional[float]
    circ_mv: Optional[float]
    amount: Optional[float]
    laggard_priority: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    data_quality: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HotThemeExpansionResult:
    themes: List[ThemeExpansionState]
    candidates: List[HotThemeExpansionCandidate]
    criteria: HotThemeExpansionCriteria
    data_quality: Dict[str, Any] = field(default_factory=dict)


class HotThemeExpansionScreener:
    """筛选热点扩散中的不过热补涨观察票。"""

    def __init__(
        self,
        *,
        snapshot_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        daily_provider: Optional[Callable[[str, int], Tuple[pd.DataFrame, str]]] = None,
        minute_provider: Optional[Callable[[str], Optional[Sequence[Dict[str, Any]]]]] = None,
        theme_universe_provider: Optional[Callable[[], ThemeUniverse]] = None,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.daily_provider = daily_provider or self._default_daily_provider
        self.minute_provider = minute_provider
        self.theme_universe_provider = theme_universe_provider

    @staticmethod
    def _default_manager():
        from data_provider.base import DataFetcherManager

        return DataFetcherManager()

    def _default_snapshot_provider(self, stock_codes: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        return self._default_manager().get_a_share_realtime_snapshot(stock_codes)

    def _default_daily_provider(self, code: str, days: int) -> Tuple[pd.DataFrame, str]:
        return self._default_manager().get_daily_data(code, days=days)

    def _load_theme_universe(self) -> Tuple[ThemeUniverse, str]:
        if self.theme_universe_provider is not None:
            return _normalize_universe(self.theme_universe_provider() or {}), "provided"
        universe = _normalize_universe(self._default_manager().get_hot_theme_universe() or {})
        if universe:
            return universe, "market_hot_board"
        return _normalize_universe(DEFAULT_THEME_UNIVERSE), "static_fallback"

    def _load_snapshot(self, stock_codes: Sequence[str]) -> List[Dict[str, Any]]:
        provider = self.snapshot_provider or self._default_snapshot_provider
        try:
            return list(provider(stock_codes) or [])
        except TypeError:
            return list(provider() or [])

    def screen(self, criteria: Optional[HotThemeExpansionCriteria] = None) -> HotThemeExpansionResult:
        criteria = criteria or HotThemeExpansionCriteria()
        universe, universe_source = self._load_theme_universe()
        snapshot = self._load_snapshot(list(universe.keys()))
        snapshot_by_code = _snapshot_by_code(snapshot)
        rows = [row for code, row in snapshot_by_code.items() if code in universe]
        theme_states = self._score_themes(rows, universe, criteria)
        theme_score_map = {state.theme: state.score for state in theme_states if state.score >= criteria.min_theme_score}

        candidates: List[HotThemeExpansionCandidate] = []
        for code, themes in universe.items():
            active_theme_scores = [theme_score_map[theme] for theme in themes if theme in theme_score_map]
            if not active_theme_scores:
                continue
            row = snapshot_by_code.get(code)
            if not row:
                continue
            candidates.append(self._score_candidate(row, themes, max(active_theme_scores), criteria))

        candidates.sort(key=lambda item: (item.laggard_priority, item.score, item.theme_score, item.change_pct or -99.0), reverse=True)
        snapshot_status = "ok" if snapshot else "source_failed"
        return HotThemeExpansionResult(
            themes=theme_states,
            candidates=candidates[: max(0, int(criteria.max_candidates))],
            criteria=criteria,
            data_quality={
                "snapshot_count": len(snapshot),
                "snapshot_status": snapshot_status,
                "snapshot_error": "realtime_snapshot_source_failed" if snapshot_status == "source_failed" else "",
                "theme_universe_count": len(universe),
                "theme_universe_source": universe_source,
                "matched_snapshot_count": len(rows),
                "active_theme_count": len(theme_score_map),
            },
        )

    def _score_themes(
        self,
        rows: Iterable[Dict[str, Any]],
        universe: ThemeUniverse,
        criteria: HotThemeExpansionCriteria,
    ) -> List[ThemeExpansionState]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            code = normalize_stock_code(str(row.get("code") or ""))
            for theme in universe.get(code, ()):
                grouped.setdefault(theme, []).append(row)

        states: List[ThemeExpansionState] = []
        for theme, members in grouped.items():
            changes = [_safe_float(row.get("change_pct")) for row in members]
            volume_ratios = [_safe_float(row.get("volume_ratio")) for row in members]
            amounts = [_safe_float(row.get("amount")) or 0.0 for row in members]
            active_count = sum(
                1
                for row in members
                if (_safe_float(row.get("change_pct")) or -99.0) > 0
                and (_safe_float(row.get("volume_ratio")) or 0.0) >= criteria.active_volume_ratio_min
            )
            up_count = sum(1 for change in changes if change is not None and change > 0)
            avg_change = _avg(changes)
            avg_volume_ratio = _avg(volume_ratios)
            total_amount = sum(amounts)
            score = 0.0
            reasons: List[str] = []
            if len(members) >= 3:
                score += 8.0
                reasons.append("成分覆盖足够")
            if active_count >= 2:
                score += min(18.0, active_count * 6.0)
                reasons.append(f"放量上涨扩散 {active_count} 只")
            if up_count >= max(1, len(members) // 3):
                score += 8.0
                reasons.append(f"上涨家数 {up_count}/{len(members)}")
            if avg_change is not None and 0.5 <= avg_change <= 5.5:
                score += 10.0
                reasons.append(f"板块均涨幅 {avg_change:.2f}% 未过热")
            elif avg_change is not None and avg_change > 7.0:
                score -= 6.0
                reasons.append(f"板块均涨幅 {avg_change:.2f}% 偏热")
            if avg_volume_ratio is not None and avg_volume_ratio >= 1.0:
                score += min(10.0, avg_volume_ratio * 4.0)
                reasons.append(f"板块均量比 {avg_volume_ratio:.2f}")
            if total_amount >= 2_000_000_000:
                score += 8.0
                reasons.append("板块成交额活跃")
            stage = _theme_stage(score, avg_change, active_count)
            states.append(
                ThemeExpansionState(
                    theme=theme,
                    score=round(max(0.0, score), 2),
                    member_count=len(members),
                    active_count=active_count,
                    up_count=up_count,
                    avg_change_pct=round(avg_change, 2) if avg_change is not None else None,
                    avg_volume_ratio=round(avg_volume_ratio, 2) if avg_volume_ratio is not None else None,
                    total_amount=total_amount,
                    stage=stage,
                    reasons=reasons,
                )
            )
        states.sort(key=lambda item: item.score, reverse=True)
        return states

    def _score_candidate(
        self,
        row: Dict[str, Any],
        themes: Sequence[str],
        theme_score: float,
        criteria: HotThemeExpansionCriteria,
    ) -> HotThemeExpansionCandidate:
        code = normalize_stock_code(str(row.get("code") or ""))
        metrics: Dict[str, Any] = {}
        reasons: List[str] = []
        warnings: List[str] = []
        data_quality: Dict[str, Any] = {}

        change_pct = _safe_float(row.get("change_pct"))
        volume_ratio = _safe_float(row.get("volume_ratio"))
        turnover_rate = _safe_float(row.get("turnover_rate"))
        circ_mv = _safe_float(row.get("circ_mv"))
        amount = _safe_float(row.get("amount"))
        price = _safe_float(row.get("price"))

        score = min(25.0, theme_score * 0.35)
        laggard_priority = 0
        reasons.append(f"热点扩散分 {theme_score:.1f}")

        if change_pct is not None:
            if criteria.laggard_change_min_pct <= change_pct <= 2.5:
                laggard_priority = 1
                score += 24.0
                reasons.append(f"涨幅 {change_pct:.2f}% 处于低位补涨观察区")
            elif 2.5 < change_pct <= criteria.laggard_change_max_pct:
                score += 14.0
                reasons.append(f"涨幅 {change_pct:.2f}% 处于趋势确认区")
            elif criteria.laggard_change_max_pct < change_pct < criteria.overheat_change_pct:
                score += 6.0
                warnings.append(f"涨幅 {change_pct:.2f}% 偏主动，等待回踩")
            elif change_pct >= criteria.overheat_change_pct:
                score -= 10.0
                warnings.append(f"涨幅 {change_pct:.2f}% 已过热")
            else:
                score += 4.0
                warnings.append(f"涨幅 {change_pct:.2f}% 偏弱，需右侧确认")

        if volume_ratio is not None:
            if 1.0 <= volume_ratio <= 2.5:
                score += 12.0
                reasons.append(f"量比 {volume_ratio:.2f} 温和放大")
            elif 2.5 < volume_ratio <= 4.5:
                score += 6.0
                warnings.append(f"量比 {volume_ratio:.2f} 偏高")
            elif volume_ratio > 4.5:
                score -= 5.0
                warnings.append(f"量比 {volume_ratio:.2f} 过高")

        if turnover_rate is not None:
            if criteria.active_turnover_min <= turnover_rate <= 8.0:
                score += 10.0
                reasons.append(f"换手 {turnover_rate:.2f}% 活跃不过热")
            elif 8.0 < turnover_rate <= criteria.active_turnover_max:
                score += 4.0
                warnings.append(f"换手 {turnover_rate:.2f}% 分歧偏大")
            elif turnover_rate > criteria.active_turnover_max:
                score -= 8.0
                warnings.append(f"换手 {turnover_rate:.2f}% 过热")

        if amount is not None and amount >= 200_000_000:
            score += 5.0
            reasons.append("成交额满足活跃度")
        if circ_mv is not None:
            circ_yi = circ_mv / 100_000_000
            if 50 <= circ_yi <= 300:
                score += 8.0
                reasons.append(f"流通市值 {circ_yi:.1f} 亿适合补涨弹性")
            elif 300 < circ_yi <= 1000:
                score += 4.0
                reasons.append(f"流通市值 {circ_yi:.1f} 亿偏中军")

        daily_metrics, daily_quality = self._daily_metrics(code, criteria)
        metrics.update(daily_metrics)
        data_quality.update(daily_quality)
        if daily_metrics.get("ma_up"):
            score += 10.0
            reasons.append("5/10/20 日均线同步向上")
        if daily_metrics.get("trend_stack"):
            score += 8.0
            reasons.append("价格处于均线多头排列")
        dist20 = _safe_float(daily_metrics.get("dist_20d_high_pct"))
        if dist20 is not None:
            if -12.0 <= dist20 <= -2.0:
                score += 8.0
                reasons.append(f"距20日高点 {dist20:.2f}% 有补涨空间")
            elif -2.0 < dist20 <= 0.5:
                score += 5.0
                reasons.append(f"贴近20日高点 {dist20:.2f}%")
            elif dist20 < -20.0:
                warnings.append(f"距20日高点 {dist20:.2f}% 趋势修复不足")

        minute_metrics = self._minute_metrics(code, criteria)
        metrics["minute_confirmation"] = minute_metrics
        if minute_metrics.get("status") == "checked":
            above_ratio = _safe_float(minute_metrics.get("above_avg_ratio")) or 0.0
            pullback_holds = bool(minute_metrics.get("pullback_holds"))
            tail_supports = bool(minute_metrics.get("tail_supports"))
            if above_ratio >= criteria.minute_above_avg_min_ratio:
                score += 8.0
                reasons.append(f"分时均线上方占比 {above_ratio:.2f}")
            else:
                warnings.append(f"分时均线上方占比 {above_ratio:.2f} 不足")
            if pullback_holds:
                score += 6.0
                reasons.append("创新高后回踩分时均线不破")
            if tail_supports:
                score += 6.0
                reasons.append("尾盘承接确认")
            if laggard_priority and not (above_ratio >= 0.75 and (pullback_holds or tail_supports)):
                laggard_priority = 0
                warnings.append("低位补涨但分时资金确认不足")
        else:
            data_quality["minute_confirmation"] = minute_metrics.get("status")

        return HotThemeExpansionCandidate(
            code=code,
            name=str(row.get("name") or ""),
            themes=list(themes),
            score=round(max(0.0, min(100.0, score)), 2),
            theme_score=round(theme_score, 2),
            laggard_priority=laggard_priority,
            price=price,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            circ_mv=circ_mv,
            amount=amount,
            metrics=metrics,
            reasons=reasons,
            warnings=warnings,
            data_quality=data_quality,
        )

    def _daily_metrics(self, code: str, criteria: HotThemeExpansionCriteria) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        try:
            history, source = self.daily_provider(code, criteria.history_days)
        except Exception as exc:
            return {}, {"daily_error": str(exc)}
        quality = {"daily_source": source}
        if history is None or history.empty or "close" not in history.columns:
            return {}, quality
        df = history.copy().sort_values("date") if "date" in history.columns else history.copy()
        close = pd.to_numeric(df["close"], errors="coerce")
        latest = _series_last(close)
        metrics: Dict[str, Any] = {}
        if latest is None:
            return metrics, quality
        for window in (5, 10, 20):
            col = f"ma{window}"
            if col not in df.columns:
                df[col] = close.rolling(window=window).mean()
        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else latest_row
        ma_values = {f"ma{window}": _safe_float(latest_row.get(f"ma{window}")) for window in (5, 10, 20)}
        ma_prev = {f"ma{window}_prev": _safe_float(prev_row.get(f"ma{window}")) for window in (5, 10, 20)}
        metrics.update(ma_values)
        metrics.update(ma_prev)
        metrics["ma_up"] = all(
            ma_values[f"ma{window}"] is not None
            and ma_prev[f"ma{window}_prev"] is not None
            and ma_values[f"ma{window}"] > ma_prev[f"ma{window}_prev"]
            for window in (5, 10, 20)
        )
        metrics["trend_stack"] = all(ma_values[f"ma{window}"] is not None for window in (5, 10, 20)) and (
            latest > ma_values["ma5"] > ma_values["ma10"] > ma_values["ma20"]
        )
        high_20 = _safe_float(close.tail(20).max())
        high_60 = _safe_float(close.tail(60).max())
        if high_20 and high_20 > 0:
            metrics["dist_20d_high_pct"] = (latest / high_20 - 1) * 100
        if high_60 and high_60 > 0:
            metrics["dist_60d_high_pct"] = (latest / high_60 - 1) * 100
        if "pct_chg" in df.columns:
            pct = pd.to_numeric(df["pct_chg"], errors="coerce").tail(20)
            metrics["limit_up_count_20d"] = int((pct >= 9.8).sum())
        return metrics, quality

    def _minute_metrics(self, code: str, criteria: HotThemeExpansionCriteria) -> Dict[str, Any]:
        if self.minute_provider is None:
            return {"status": "not_checked"}
        rows = list(self.minute_provider(code) or [])
        points = [(_safe_float(row.get("price") or row.get("close")), _safe_float(row.get("avg_price") or row.get("average_price"))) for row in rows]
        points = [point for point in points if point[0] is not None and point[1] is not None]
        if len(points) < 10:
            return {"status": "insufficient_data"}
        prices = [point[0] for point in points]
        avgs = [point[1] for point in points]
        above_ratio = sum(1 for price, avg in points if price >= avg * 0.998) / len(points)
        high_index = max(range(len(prices)), key=lambda idx: prices[idx])
        after_high = list(zip(prices[high_index:], avgs[high_index:]))
        break_count = sum(1 for price, avg in after_high if price < avg * 0.998)
        tail_len = min(max(3, int(criteria.tail_minutes)), len(prices))
        tail_prices = prices[-tail_len:]
        tail_gain = (tail_prices[-1] / tail_prices[0] - 1) * 100 if tail_prices[0] else 0.0
        tail_high = max(tail_prices)
        tail_close_near_high_pct = (tail_prices[-1] / tail_high - 1) * 100 if tail_high else 0.0
        tail_directions = [1 if curr > prev else -1 if curr < prev else 0 for prev, curr in zip(tail_prices, tail_prices[1:])]
        tail_directions = [direction for direction in tail_directions if direction]
        reversal_count = sum(1 for prev, curr in zip(tail_directions, tail_directions[1:]) if prev != curr)
        return {
            "status": "checked",
            "above_avg_ratio": above_ratio,
            "pullback_break_count": break_count,
            "pullback_holds": break_count <= 3,
            "tail_gain_pct": tail_gain,
            "tail_reversal_count": reversal_count,
            "tail_close_near_high_pct": tail_close_near_high_pct,
            "tail_supports": tail_gain >= 0 and tail_close_near_high_pct >= -0.8 and reversal_count <= max(8, tail_len // 3),
        }


def screen_hot_theme_expansion(criteria: Optional[HotThemeExpansionCriteria] = None) -> HotThemeExpansionResult:
    criteria = criteria or HotThemeExpansionCriteria()
    daily_provider = None
    minute_provider = None
    from src.config import get_config

    if get_config().tushare_token:
        from src.services.daily_history_cache_provider import DailyHistoryCacheProvider

        daily_provider = DailyHistoryCacheProvider()
    from src.services.intraday_minute_provider import IntradayMinuteCacheProvider

    minute_provider = IntradayMinuteCacheProvider()
    return HotThemeExpansionScreener(daily_provider=daily_provider, minute_provider=minute_provider).screen(criteria)


def result_to_dict(result: HotThemeExpansionResult) -> Dict[str, Any]:
    return {
        "themes": [asdict(item) for item in result.themes],
        "candidates": [asdict(item) for item in result.candidates],
        "criteria": asdict(result.criteria),
        "data_quality": result.data_quality,
    }


def _normalize_universe(universe: ThemeUniverse) -> ThemeUniverse:
    normalized: Dict[str, Sequence[str]] = {}
    for code, themes in universe.items():
        normalized_code = normalize_stock_code(str(code or ""))
        theme_list = tuple(str(theme).strip() for theme in themes if str(theme).strip())
        if normalized_code and theme_list:
            normalized[normalized_code] = theme_list
    return normalized


def _snapshot_by_code(snapshot: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for row in snapshot:
        code = normalize_stock_code(str(row.get("code") or ""))
        if code:
            rows[code] = {**row, "code": code}
    return rows


def _theme_stage(score: float, avg_change: Optional[float], active_count: int) -> str:
    if score >= 45 and active_count >= 3:
        return "扩散"
    if score >= 32 and active_count >= 1:
        return "启动"
    if avg_change is not None and avg_change > 7:
        return "过热"
    return "观察"


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


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _series_last(series: pd.Series) -> Optional[float]:
    clean = series.dropna()
    if clean.empty:
        return None
    return _safe_float(clean.iloc[-1])
