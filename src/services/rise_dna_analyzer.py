# -*- coding: utf-8 -*-
"""涨跌基因分析器 — 提取上涨股票的共同特征，发现市场规律。"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board

logger = logging.getLogger(__name__)


@dataclass
class RiseDNAConfig:
    min_change_pct: float = 3.0       # 涨幅阈值（%）
    lookback_days: int = 15           # 回溯加载天数
    analyze_days: int = 5             # 分析最近几个交易日
    min_consecutive_days: int = 3     # 连涨最少天数
    concept_top_n: int = 20           # 热点概念取前N个


@dataclass
class DayAnalysis:
    date: str
    total_stocks: int
    rising_count: int
    rising_pct: float
    avg_change: float
    concept_top: List[Tuple[str, int]]
    ma_alignment: Dict[str, int]
    volume_ratio_dist: Dict[str, int]
    price_range_dist: Dict[str, int]
    amplitude_dist: Dict[str, int]


@dataclass
class RiseDNAReport:
    config: RiseDNAConfig
    day_analyses: List[DayAnalysis] = field(default_factory=list)
    consecutive_stocks: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_concept_top: List[Tuple[str, int]] = field(default_factory=list)
    consecutive_traits: Dict[str, Any] = field(default_factory=dict)
    cross_day_patterns: List[str] = field(default_factory=list)

    @property
    def dates(self) -> List[str]:
        return [d.date for d in self.day_analyses]


class RiseDNAAnalyzer:
    """涨跌基因分析 — 从上涨股票中提取共同特征。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        theme_universe_provider: Optional[Callable[[], Dict[str, Sequence[str]]]] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._theme_universe_provider = theme_universe_provider

    def analyze(self, config: Optional[RiseDNAConfig] = None) -> RiseDNAReport:
        config = config or RiseDNAConfig()
        report = RiseDNAReport(config=config)

        # Step 1: 批量加载 + 计算指标
        df = self.db.get_bulk_daily_data(days=config.lookback_days)
        if df.empty:
            logger.warning("[RiseDNA] 无日线数据")
            return report

        metrics_df = self._compute_metrics(df)
        if metrics_df.empty:
            logger.warning("[RiseDNA] 计算指标后无数据")
            return report

        # 获取交易日列表（最近的 N 天）
        trading_dates = sorted(metrics_df["date"].unique(), reverse=True)
        trading_dates = trading_dates[:config.analyze_days]

        if not trading_dates:
            logger.warning("[RiseDNA] 无交易日数据")
            return report

        # Step 2: 加载概念
        theme_universe = self._load_theme_universe(config.concept_top_n)

        # Step 3: 逐日分析
        for d in reversed(trading_dates):
            day_analysis = self._analyze_one_day(metrics_df, d, config, theme_universe)
            if day_analysis:
                report.day_analyses.append(day_analysis)

        # Step 4: 连涨股分析
        self._analyze_consecutive(metrics_df, trading_dates, config, theme_universe, report)

        # Step 5: 跨日规律
        self._find_cross_day_patterns(report)

        return report

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
        results = []
        for code, group in df.groupby("code"):
            if is_excluded_board(code):
                continue
            g = group.sort_values("date").copy()
            if len(g) < 5:
                continue

            close = g["close"]
            volume = g["volume"]

            g["ma5"] = close.rolling(window=5, min_periods=5).mean()
            g["ma10"] = close.rolling(window=10, min_periods=10).mean()
            g["ma20"] = close.rolling(window=20, min_periods=20).mean()

            avg_vol_5 = volume.rolling(window=5, min_periods=5).mean().shift(1)
            g["vol_ratio"] = (volume / avg_vol_5).where(avg_vol_5 > 0)

            # 振幅
            prev_close = close.shift(1)
            g["amplitude"] = ((g["high"] - g["low"]) / prev_close * 100).where(prev_close > 0)

            # 连涨天数
            is_up = (close > close.shift(1)).astype(int)
            consec = []
            count = 0
            for val in is_up:
                if val == 1:
                    count += 1
                else:
                    count = 0
                consec.append(count)
            g["consecutive_up"] = consec

            results.append(g)

        if not results:
            return pd.DataFrame()
        return pd.concat(results, ignore_index=True)

    # ------------------------------------------------------------------
    # One day analysis
    # ------------------------------------------------------------------

    def _analyze_one_day(
        self,
        metrics_df: pd.DataFrame,
        date_val: str,
        config: RiseDNAConfig,
        theme_universe: Dict[str, List[str]],
    ) -> Optional[DayAnalysis]:
        day_df = metrics_df[metrics_df["date"] == date_val].copy()
        if day_df.empty:
            return None

        total = len(day_df)
        rising = day_df[day_df["pct_chg"] >= config.min_change_pct]
        rising_count = len(rising)

        if rising_count == 0:
            return DayAnalysis(
                date=str(date_val)[:10],
                total_stocks=total,
                rising_count=0,
                rising_pct=0.0,
                avg_change=0.0,
                concept_top=[],
                ma_alignment={},
                volume_ratio_dist={},
                price_range_dist={},
                amplitude_dist={},
            )

        avg_change = rising["pct_chg"].mean()

        # 概念集中度
        concept_counter: Counter = Counter()
        for _, row in rising.iterrows():
            code = row["code"]
            themes = theme_universe.get(code, [])
            for t in themes:
                concept_counter[t] += 1
        concept_top = concept_counter.most_common(10)

        # 均线排列
        ma_alignment = self._classify_ma_alignment(rising)

        # 量比分档
        volume_ratio_dist = self._classify_volume_ratio(rising)

        # 价格区间
        price_range_dist = self._classify_price_range(rising)

        # 振幅分布
        amplitude_dist = self._classify_amplitude(rising)

        return DayAnalysis(
            date=str(date_val)[:10],
            total_stocks=total,
            rising_count=rising_count,
            rising_pct=round(rising_count / total * 100, 1) if total > 0 else 0.0,
            avg_change=round(avg_change, 2),
            concept_top=concept_top,
            ma_alignment=ma_alignment,
            volume_ratio_dist=volume_ratio_dist,
            price_range_dist=price_range_dist,
            amplitude_dist=amplitude_dist,
        )

    @staticmethod
    def _classify_ma_alignment(df: pd.DataFrame) -> Dict[str, int]:
        """均线排列分类：多头(价>MA5>MA10>MA20)、中性、空头。"""
        bull = 0
        bear = 0
        neutral = 0
        for _, row in df.iterrows():
            c = row.get("close", 0)
            ma5 = row.get("ma5")
            ma10 = row.get("ma10")
            ma20 = row.get("ma20")
            if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
                neutral += 1
                continue
            if c >= ma5 >= ma10 >= ma20:
                bull += 1
            elif c <= ma5 <= ma10 <= ma20:
                bear += 1
            else:
                neutral += 1
        return {"多头": bull, "中性": neutral, "空头": bear}

    @staticmethod
    def _classify_volume_ratio(df: pd.DataFrame) -> Dict[str, int]:
        shrink = int(((df["vol_ratio"] < 0.8) & df["vol_ratio"].notna()).sum())
        flat = int(((df["vol_ratio"] >= 0.8) & (df["vol_ratio"] <= 1.5)).sum())
        expand = int((df["vol_ratio"] > 1.5).sum())
        unknown = int(df["vol_ratio"].isna().sum())
        result = {"放量(>1.5)": expand, "平量(0.8-1.5)": flat, "缩量(<0.8)": shrink}
        if unknown > 0:
            result["未知"] = unknown
        return result

    @staticmethod
    def _classify_price_range(df: pd.DataFrame) -> Dict[str, int]:
        bins = [0, 10, 30, 50, 100, float("inf")]
        labels = ["<10元", "10-30元", "30-50元", "50-100元", ">100元"]
        cut = pd.cut(df["close"], bins=bins, labels=labels, right=False)
        return dict(cut.value_counts())

    @staticmethod
    def _classify_amplitude(df: pd.DataFrame) -> Dict[str, int]:
        amp = df["amplitude"].dropna()
        bins = [0, 3, 5, 8, float("inf")]
        labels = ["<3%", "3-5%", "5-8%", ">8%"]
        if amp.empty:
            return {l: 0 for l in labels}
        cut = pd.cut(amp, bins=bins, labels=labels, right=False)
        return dict(cut.value_counts())

    # ------------------------------------------------------------------
    # Consecutive rise analysis
    # ------------------------------------------------------------------

    def _analyze_consecutive(
        self,
        metrics_df: pd.DataFrame,
        trading_dates: List[str],
        config: RiseDNAConfig,
        theme_universe: Dict[str, List[str]],
        report: RiseDNAReport,
    ) -> None:
        latest_date = trading_dates[0]
        latest_df = metrics_df[metrics_df["date"] == latest_date].copy()

        consec_df = latest_df[latest_df["consecutive_up"] >= config.min_consecutive_days].copy()
        if consec_df.empty:
            return

        stocks = []
        concept_counter: Counter = Counter()
        for _, row in consec_df.iterrows():
            code = row["code"]
            themes = theme_universe.get(code, [])
            for t in themes:
                concept_counter[t] += 1
            stocks.append({
                "code": code,
                "close": round(row["close"], 2),
                "pct_chg": round(row.get("pct_chg", 0) or 0, 2),
                "consecutive_days": int(row["consecutive_up"]),
                "vol_ratio": round(row.get("vol_ratio", 0) or 0, 2),
                "themes": list(themes),
            })

        stocks.sort(key=lambda s: (-s["consecutive_days"], -s["pct_chg"]))

        # 连涨股共同特征
        ma_alignment = self._classify_ma_alignment(consec_df)
        total_consec = len(consec_df)

        traits: Dict[str, Any] = {}
        if total_consec > 0:
            traits["ma_alignment"] = ma_alignment
            traits["ma_bull_pct"] = round(ma_alignment.get("多头", 0) / total_consec * 100, 1)
            traits["vol_ratio_avg"] = round(consec_df["vol_ratio"].mean(), 2)
            traits["vol_expand_pct"] = round(
                (consec_df["vol_ratio"] > 1.2).sum() / total_consec * 100, 1
            )
            price_cut = pd.cut(
                consec_df["close"],
                bins=[0, 10, 30, 50, 100, float("inf")],
                labels=["<10", "10-30", "30-50", "50-100", ">100"],
                right=False,
            )
            price_mode = price_cut.mode()
            traits["price_mode"] = str(price_mode.iloc[0]) if not price_mode.empty else "N/A"

        report.consecutive_stocks = stocks
        report.consecutive_concept_top = concept_counter.most_common(10)
        report.consecutive_traits = traits

    # ------------------------------------------------------------------
    # Cross-day patterns
    # ------------------------------------------------------------------

    @staticmethod
    def _find_cross_day_patterns(report: RiseDNAReport) -> None:
        patterns: List[str] = []
        analyses = report.day_analyses
        if len(analyses) < 2:
            return

        # 概念延续性：哪些概念在多天都出现
        concept_by_day: Dict[str, List[str]] = {}
        for a in analyses:
            top_concepts = {c for c, _ in a.concept_top[:5]}
            concept_by_day[a.date] = top_concepts

        all_concepts = set()
        for s in concept_by_day.values():
            all_concepts.update(s)

        for concept in all_concepts:
            days_present = sum(1 for s in concept_by_day.values() if concept in s)
            if days_present >= max(2, len(analyses) // 2 + 1):
                patterns.append(
                    f"「{concept}」连续 {days_present}/{len(analyses)} 天出现在涨幅概念 TOP5"
                )

        # 均线排列趋势
        bull_pcts = []
        for a in analyses:
            total_ma = sum(a.ma_alignment.values())
            if total_ma > 0:
                bull_pcts.append(a.ma_alignment.get("多头", 0) / total_ma * 100)

        if len(bull_pcts) >= 2 and bull_pcts[-1] > bull_pcts[0] + 5:
            trend = " → ".join(f"{p:.0f}%" for p in bull_pcts)
            patterns.append(f"多头排列占比逐日上升: {trend}")
        elif len(bull_pcts) >= 2 and bull_pcts[-1] < bull_pcts[0] - 5:
            trend = " → ".join(f"{p:.0f}%" for p in bull_pcts)
            patterns.append(f"多头排列占比逐日下降: {trend}")

        # 上涨占比趋势
        rising_pcts = [a.rising_pct for a in analyses if a.rising_pct > 0]
        if len(rising_pcts) >= 2 and rising_pcts[-1] > rising_pcts[0] * 1.5:
            patterns.append(
                f"上涨股票占比扩大: {rising_pcts[0]:.1f}% → {rising_pcts[-1]:.1f}%"
            )

        report.cross_day_patterns = patterns

    # ------------------------------------------------------------------
    # Theme universe
    # ------------------------------------------------------------------

    def _load_theme_universe(self, top_n: int) -> Dict[str, List[str]]:
        if self._theme_universe_provider:
            return dict(self._theme_universe_provider())

        try:
            from data_provider.base import get_data_fetcher_manager
            manager = get_data_fetcher_manager()
            universe = manager.get_hot_theme_universe(n=top_n, max_members_per_theme=100)
            if universe:
                logger.info("[RiseDNA] 热点概念加载完成: %d 只股票有概念标注", len(universe))
                return dict(universe)
        except Exception as exc:
            logger.warning("[RiseDNA] 获取概念板块失败: %s", exc)

        return {}

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_report(report: RiseDNAReport) -> str:
        lines: List[str] = []
        cfg = report.config
        dates = report.dates

        lines.append("=" * 60)
        lines.append("              涨跌基因分析报告")
        lines.append("=" * 60)

        if dates:
            lines.append(f"分析周期: {dates[0]} ~ {dates[-1]} ({len(dates)}个交易日)")
        lines.append(f"涨幅阈值: >= {cfg.min_change_pct:.2f}%")
        lines.append("")

        # 逐日分析
        for a in reversed(report.day_analyses):
            tag = " (最近交易日)" if a.date == dates[-1] else ""
            lines.append(f"----- {a.date}{tag} -----")
            lines.append(
                f"上涨股票: {a.rising_count}/{a.total_stocks} "
                f"({a.rising_pct:.1f}%)  平均涨幅: +{a.avg_change:.2f}%"
            )
            lines.append("")

            # 概念集中度
            if a.concept_top:
                lines.append("  [概念集中度 TOP5]")
                for name, count in a.concept_top[:5]:
                    pct = count / a.rising_count * 100 if a.rising_count > 0 else 0
                    bar = "#" * int(pct / 2)
                    lines.append(f"    {name:<12} {count:>3}只 ({pct:>5.1f}%)  {bar}")
                lines.append("")

            # 均线排列
            total_ma = sum(a.ma_alignment.values())
            if total_ma > 0:
                lines.append("  [均线排列]")
                for label, count in a.ma_alignment.items():
                    pct = count / total_ma * 100
                    lines.append(f"    {label}: {count}只 ({pct:.1f}%)")
                lines.append("")

            # 量价配合
            total_vol = sum(a.volume_ratio_dist.values())
            if total_vol > 0:
                lines.append("  [量价配合]")
                for label, count in a.volume_ratio_dist.items():
                    pct = count / total_vol * 100
                    lines.append(f"    {label}: {count}只 ({pct:.1f}%)")
                lines.append("")

            # 价格区间
            total_price = sum(a.price_range_dist.values())
            if total_price > 0:
                lines.append("  [价格区间]")
                for label in ["<10元", "10-30元", "30-50元", "50-100元", ">100元"]:
                    count = a.price_range_dist.get(label, 0)
                    pct = count / total_price * 100 if total_price > 0 else 0
                    lines.append(f"    {label:<10} {count:>3}只 ({pct:>5.1f}%)")
                lines.append("")

            # 振幅
            total_amp = sum(a.amplitude_dist.values())
            if total_amp > 0:
                lines.append("  [振幅分布]")
                for label in ["<3%", "3-5%", "5-8%", ">8%"]:
                    count = a.amplitude_dist.get(label, 0)
                    pct = count / total_amp * 100 if total_amp > 0 else 0
                    lines.append(f"    {label:<8} {count:>3}只 ({pct:>5.1f}%)")
                lines.append("")

        # 连涨股分析
        consec = report.consecutive_stocks
        if consec:
            lines.append(f"----- 连涨 >= {cfg.min_consecutive_days}天 -----")
            lines.append(f"连续上涨 >= {cfg.min_consecutive_days}天: {len(consec)}只")
            lines.append("")

            if report.consecutive_concept_top:
                lines.append("  [连涨股概念集中度 TOP5]")
                for name, count in report.consecutive_concept_top[:5]:
                    pct = count / len(consec) * 100
                    lines.append(f"    {name:<12} {count:>3}只 ({pct:>5.1f}%)")
                lines.append("")

            traits = report.consecutive_traits
            if traits:
                lines.append("  [共同特征]")
                if "ma_bull_pct" in traits:
                    lines.append(f"    多头排列占比: {traits['ma_bull_pct']:.1f}%")
                if "vol_ratio_avg" in traits:
                    lines.append(f"    平均量比: {traits['vol_ratio_avg']:.2f}")
                if "vol_expand_pct" in traits:
                    lines.append(f"    量比>1.2占比: {traits['vol_expand_pct']:.1f}%")
                if "price_mode" in traits:
                    lines.append(f"    集中价格区间: {traits['price_mode']}元")
                lines.append("")

            # 列出连涨股
            lines.append("  [连涨股列表 TOP20]")
            lines.append(f"  {'代码':<8} {'收盘':>8} {'涨幅':>7} {'连涨天数':>6} {'量比':>6}  概念")
            lines.append("  " + "-" * 70)
            for s in consec[:20]:
                themes_str = ",".join(s["themes"][:3]) if s["themes"] else "-"
                lines.append(
                    f"  {s['code']:<8} {s['close']:>8.2f} {s['pct_chg']:>+6.2f}% "
                    f"{s['consecutive_days']:>5}天 {s['vol_ratio']:>6.2f}  {themes_str}"
                )
            lines.append("")

        # 跨日规律
        if report.cross_day_patterns:
            lines.append("----- 跨日规律 -----")
            for p in report.cross_day_patterns:
                lines.append(f"  * {p}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)
