# -*- coding: utf-8 -*-
"""横盘选股器 — 找出"最近 N 天内曾经横盘过"的股票。

核心定义（与用户对齐）：
- 不限制价格本身（无振幅阈值、无均线收敛阈值、无箱体宽度阈值）
- 不限制量能
- 不要求"现在还在横盘"，而是"lookback_days 内出现过横盘"
- 判定维度 1：子窗口斜率 ≈ 0（slope/close_mean ≤ slope_max_pct）
- 判定维度 2：子窗口起起伏伏（最长连续同向天数 < max_consecutive_days）
- 过滤 ST/*ST/退市等"不正经"股票
- 不依赖概念数据（横盘是价格行为，与概念无关）

算法：在 lookback_days 窗口内，用大小 min_sideways_days 的滑动子窗口扫描，
找出最长一段连续满足双维度的区间，输出横盘段长度 + 距今天数。

与 MA10PullbackScreener 同构，但方法名/类名全部前缀 sideways/screen_sideways_*，
不影响旧算法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board, is_excluded_by_name

logger = logging.getLogger(__name__)


@dataclass
class SidewaysCriteria:
    """横盘判定参数。"""
    lookback_days: int = 30                # 扫描总窗口（默认最近 30 天）
    slope_max_pct: float = 0.1             # 子窗口每日斜率/close均值 ≤ 0.1%
    max_consecutive_days: int = 5          # 子窗口最长连续同向天数 < 5（超过即非横盘）
    min_sideways_len: int = 5              # 横盘段实际天数必须 > 此值（默认 >5 天）
    min_avg_amount_yi: float = 1.0         # 横盘段日均成交额 ≥ 此值（亿元，默认 1 亿）
    max_candidates: int = 50               # 最多返回候选


@dataclass
class SidewaysCandidate:
    """横盘候选股。"""
    code: str
    name: str = ""
    close: float = 0.0                     # 当前收盘价
    sideways_start_date: str = ""          # 横盘段起始日期 YYYY-MM-DD
    sideways_end_date: str = ""            # 横盘段结束日期 YYYY-MM-DD
    sideways_len: int = 0                  # 横盘段实际天数
    days_since_sideways: int = 0           # 横盘段结束距今天数（0=今天还在横盘）
    sideways_slope_pct: float = 0.0        # 横盘段子窗口斜率（%/天）
    sideways_max_streak: int = 0           # 横盘段子窗口最长连续同向天数
    current_vs_high_pct: float = 0.0       # 当前价相对横盘段最高价的跌幅%
    current_vs_low_pct: float = 0.0        # 当前价相对横盘段最低价的涨幅%
    seg_avg_amount_yi: float = 0.0         # 横盘段日均成交额（亿元）
    # 资金评价（横盘段累计）
    ff_total_net: Optional[float] = None   # 横盘段累计总净流入(万元)；None=无数据
    ff_big_net: Optional[float] = None     # 横盘段累计大单净流入(万元)
    ff_small_net: Optional[float] = None   # 横盘段累计小单净流入(万元)
    ff_eval: str = "-"                     # 资金评价: 主力吸筹/主力派发/大进小出/大出小进/中性
    # 洗盘分（横盘段最后一日）
    wash_score: int = 0                    # 洗盘分 0-17
    wash_detail: str = ""                  # 洗盘分组成 tags


class SidewaysScreener:
    """横盘选股（找最近 N 天内曾横盘过的股票）。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._stock_name_provider = stock_name_provider

    def screen(self, criteria: Optional[SidewaysCriteria] = None) -> List[SidewaysCandidate]:
        """跑横盘选股。"""
        criteria = criteria or SidewaysCriteria()

        # 滑动子窗口大小：与 max_consecutive_days 自动绑定
        # max_consecutive_days=5 → scan_window=7（≥5+2 留余量，能识别出连续 5 天同向）
        scan_window = max(criteria.max_consecutive_days + 2, 7)

        # Step 1: 批量加载日线（多取 buffer 保证窗口足量）
        df = self.db.get_bulk_daily_data(days=criteria.lookback_days + 5)
        if df.empty:
            logger.warning("[SidewaysScreen] 无日线数据")
            return []

        # Step 2: 滑动扫描找最长横盘段
        metrics_df = self._compute_sideways_metrics(df, criteria, scan_window)
        if metrics_df.empty:
            logger.info("[SidewaysScreen] 指标计算后为空")
            return []

        # Step 3: 过滤（出现过横盘段且长度 > min_sideways_len）
        filtered = self._filter_sideways(metrics_df, criteria)
        if filtered.empty:
            logger.info("[SidewaysScreen] 无符合条件的股票")
            return []

        # Step 3.5: 批量加载候选股横盘段范围内的资金流（用于资金评价）
        ff_map = self._load_fund_flow_for_segments(filtered)

        # Step 4: 构建候选 + 排序
        candidates = self._build_sideways_candidates(filtered, ff_map, criteria)

        logger.info("[SidewaysScreen] 筛选结果: %d 只", len(candidates))
        return candidates

    # ------------------------------------------------------------------
    # Metrics — 滑动扫描
    # ------------------------------------------------------------------

    def _compute_sideways_metrics(
        self, df: pd.DataFrame, criteria: SidewaysCriteria, scan_window: int,
    ) -> pd.DataFrame:
        """按 code 分组，在 lookback_days 窗口内滑动扫描找最长横盘段。

        Args:
            scan_window: 滑动子窗口大小（自动 = max_consecutive_days+2，至少 7）
        """
        results = []
        lookback = criteria.lookback_days
        win = scan_window

        for code, group in df.groupby("code"):
            # 排除创业板/科创板/北交所
            if is_excluded_board(code):
                continue

            # 排除 ST/*ST/退市等"不正经"股票（早期过滤避免无效计算）
            name = self._get_stock_name(code)
            if is_excluded_by_name(name):
                continue

            g = group.sort_values("date").copy()
            if len(g) < win:
                continue

            # 取最近 lookback_days 天（不足则取全部）
            window = g.tail(lookback) if len(g) >= lookback else g
            closes = window["close"].to_numpy(dtype=float)
            amounts_all = window["amount"].to_numpy(dtype=float) if "amount" in window.columns else None
            dates_raw = window["date"].to_numpy()
            n = len(closes)
            if n < win:
                continue

            # 日期格式化为 YYYY-MM-DD
            dates = pd.to_datetime(dates_raw).strftime("%Y-%m-%d").to_numpy()

            # 滑动扫描：标记每个终点 i 是否为"过去 win 天满足双维度"
            is_sideways_day = np.zeros(n, dtype=bool)
            sub_metrics: Dict[int, Tuple[float, int]] = {}
            for i in range(win - 1, n):
                sub = closes[i - win + 1 : i + 1]
                slope_pct = SidewaysScreener._slope_pct(sub)
                max_streak = SidewaysScreener._max_consecutive_streak(sub)
                sub_metrics[i] = (slope_pct, max_streak)
                if abs(slope_pct) <= criteria.slope_max_pct and max_streak < criteria.max_consecutive_days:
                    is_sideways_day[i] = True

            # 找最长连续 True 段
            max_run_len = 0
            max_run_end = -1
            cur_run = 0
            cur_start = 0
            best_start = 0
            for i in range(n):
                if is_sideways_day[i]:
                    if cur_run == 0:
                        cur_start = i
                    cur_run += 1
                    if cur_run > max_run_len:
                        max_run_len = cur_run
                        max_run_end = i
                        best_start = cur_start
                else:
                    cur_run = 0

            if max_run_len == 0:
                continue  # 没出现过横盘

            # 横盘段实际覆盖区间 [best_start - win + 1, max_run_end]
            seg_start_idx = best_start - win + 1
            seg_end_idx = max_run_end
            seg_closes = closes[seg_start_idx : seg_end_idx + 1]
            sideways_len = len(seg_closes)
            days_since_sideways = (n - 1) - max_run_end

            # 横盘段日均成交额（单位：元 → 亿元）
            if amounts_all is not None and sideways_len > 0:
                seg_amounts = amounts_all[seg_start_idx : seg_end_idx + 1]
                valid = [a for a in seg_amounts if a and a > 0]
                seg_avg_amount_yi = (
                    sum(valid) / len(valid) / 1e8 if valid else 0.0
                )
            else:
                seg_avg_amount_yi = 0.0

            # 横盘段起止日期
            seg_start_date = str(dates[seg_start_idx])
            seg_end_date = str(dates[seg_end_idx])

            # 取横盘段最后一个子窗口的指标（代表横盘结束时的状态）
            seg_slope_pct, seg_max_streak = sub_metrics[max_run_end]

            # 当前价相对横盘段最高/最低的位置
            seg_high = float(seg_closes.max())
            seg_low = float(seg_closes.min())
            current = float(closes[-1])
            current_vs_high_pct = (
                (current - seg_high) / seg_high * 100 if seg_high > 0 else 0.0
            )
            current_vs_low_pct = (
                (current - seg_low) / seg_low * 100 if seg_low > 0 else 0.0
            )

            # 洗盘分（横盘段最后一日）：复用 PatternScreener._compute_wash_score
            # 算法需要 ≥20 天历史（MA20），传入完整 g（窗口前的历史也包含）
            wash_score, wash_detail = self._compute_wash_at_segment_end(g, window, seg_end_idx)

            results.append({
                "code": code,
                "name": name,
                "close": current,
                "sideways_start_date": seg_start_date,
                "sideways_end_date": seg_end_date,
                "sideways_len": int(sideways_len),
                "days_since_sideways": int(days_since_sideways),
                "sideways_slope_pct": seg_slope_pct,
                "sideways_max_streak": int(seg_max_streak),
                "current_vs_high_pct": current_vs_high_pct,
                "current_vs_low_pct": current_vs_low_pct,
                "seg_avg_amount_yi": round(seg_avg_amount_yi, 3),
                "wash_score": wash_score,
                "wash_detail": wash_detail,
            })

        if not results:
            return pd.DataFrame()
        return pd.DataFrame(results)

    @staticmethod
    def _compute_wash_at_segment_end(
        g: pd.DataFrame, window: pd.DataFrame, seg_end_idx: int,
    ) -> Tuple[int, str]:
        """计算横盘段最后一日的洗盘分（复用 PatternScreener._compute_wash_score）。

        PatternScreener._compute_wash_score 需要传入以"评分日"结尾的 OHLCV DataFrame，
        且需要 ≥20 天历史算 MA20。我们从 g（全部历史）里截取横盘段最后一日 + 前 30 天。
        """
        try:
            from src.services.pattern_screener import PatternScreener
        except Exception as exc:
            logger.debug("[SidewaysScreen] 导入 PatternScreener 失败: %s", exc)
            return 0, ""

        # window 在 g 中的起始位置
        if len(g) < len(window):
            return 0, ""
        start_in_g = len(g) - len(window)
        end_in_g = start_in_g + seg_end_idx

        # 取横盘段最后一日 + 前 30 天（覆盖 MA20 + buffer）
        slice_start = max(0, end_in_g - 30)
        sub_g = g.iloc[slice_start : end_in_g + 1].reset_index(drop=True)
        if len(sub_g) < 20:
            return 0, ""

        try:
            return PatternScreener._compute_wash_score(sub_g)
        except Exception as exc:
            logger.debug("[SidewaysScreen] wash_score 计算异常: %s", exc)
            return 0, ""

    # ------------------------------------------------------------------
    # 双维度原子计算
    # ------------------------------------------------------------------

    @staticmethod
    def _slope_pct(closes: np.ndarray) -> float:
        """线性回归斜率 / 均值 × 100（单位：%/天）。"""
        if len(closes) < 2:
            return 0.0
        x = np.arange(len(closes), dtype=float)
        try:
            slope = float(np.polyfit(x, closes, 1)[0])
        except (np.linalg.LinAlgError, ValueError):
            return 0.0
        mean = float(closes.mean())
        return (slope / mean * 100) if mean > 0 else 0.0

    @staticmethod
    def _max_consecutive_streak(closes: np.ndarray) -> int:
        """最长连续同向天数（平盘重置）。"""
        if len(closes) < 2:
            return 0
        signs = np.sign(np.diff(closes))
        max_streak = 0
        cur = 0
        prev = 0.0
        for s in signs:
            if s == 0:
                cur = 0
                prev = 0
                continue
            if s == prev:
                cur += 1
            else:
                cur = 1
                prev = float(s)
            if cur > max_streak:
                max_streak = cur
        return max_streak

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_sideways(df: pd.DataFrame, criteria: SidewaysCriteria) -> pd.DataFrame:
        """保留出现过横盘段且长度 > min_sideways_len、日均成交额 ≥ min_avg_amount_yi 的股票。"""
        df = df.dropna(subset=["sideways_len", "seg_avg_amount_yi"])
        mask_len = df["sideways_len"] > criteria.min_sideways_len
        mask_amount = df["seg_avg_amount_yi"] >= criteria.min_avg_amount_yi
        return df[mask_len & mask_amount].copy()

    # ------------------------------------------------------------------
    # Build candidates
    # ------------------------------------------------------------------

    def _build_sideways_candidates(
        self,
        filtered: pd.DataFrame,
        ff_map: Dict[str, Dict],
        criteria: SidewaysCriteria,
    ) -> List[SidewaysCandidate]:
        """构建候选、排序（横盘长度降序，距今升序）。"""
        candidates = []
        for _, row in filtered.iterrows():
            code = row["code"]
            # 复用 metrics 阶段已查到的 name（避免重复查询）
            name = row.get("name") or self._get_stock_name(code)

            # 横盘段累计资金（来自 ff_map，按起止日期聚合）
            seg_ff = ff_map.get(code, {})
            ff_total_net = seg_ff.get("total_net")
            ff_big_net = seg_ff.get("big_net")
            ff_small_net = seg_ff.get("small_net")
            ff_eval = self._evaluate_fund_flow(ff_total_net, ff_big_net, ff_small_net)

            candidates.append(SidewaysCandidate(
                code=code,
                name=name,
                close=round(row["close"], 2),
                sideways_start_date=str(row["sideways_start_date"]),
                sideways_end_date=str(row["sideways_end_date"]),
                sideways_len=int(row["sideways_len"]),
                days_since_sideways=int(row["days_since_sideways"]),
                sideways_slope_pct=round(float(row["sideways_slope_pct"]), 4),
                sideways_max_streak=int(row["sideways_max_streak"]),
                current_vs_high_pct=round(float(row["current_vs_high_pct"]), 2),
                current_vs_low_pct=round(float(row["current_vs_low_pct"]), 2),
                seg_avg_amount_yi=round(float(row.get("seg_avg_amount_yi") or 0.0), 3),
                ff_total_net=ff_total_net,
                ff_big_net=ff_big_net,
                ff_small_net=ff_small_net,
                ff_eval=ff_eval,
                wash_score=int(row.get("wash_score") or 0),
                wash_detail=str(row.get("wash_detail") or ""),
            ))

        # 排序：距今天升序（越近越靠前），大单降序，小单降序
        # None 值视为负无穷（排最后）
        candidates.sort(key=lambda c: (
            c.days_since_sideways,
            -(c.ff_big_net if c.ff_big_net is not None else float('-inf')),
            -(c.ff_small_net if c.ff_small_net is not None else float('-inf')),
        ))
        return candidates[:criteria.max_candidates]

    # ------------------------------------------------------------------
    # 资金流加载与评价
    # ------------------------------------------------------------------

    def _load_fund_flow_for_segments(
        self, filtered: pd.DataFrame,
    ) -> Dict[str, Dict]:
        """批量加载候选股横盘段范围内的资金流，按股票聚合累计值。

        Returns:
            {code: {"total_net": float|None, "big_net": ..., "small_net": ...}}
            单位：万元；若该股在横盘段内无任何资金流数据，三个字段均为 None。
        """
        from sqlalchemy import text

        result: Dict[str, Dict] = {}
        if filtered.empty:
            return result

        # 收集 (code, start, end) 三元组
        segments: List[Tuple[str, str, str]] = []
        for _, row in filtered.iterrows():
            segments.append((
                str(row["code"]),
                str(row["sideways_start_date"]),
                str(row["sideways_end_date"]),
            ))

        # 统一查全市场候选股 × 整段最大日期范围（避免 N 次 SQL）
        all_codes = [s[0] for s in segments]
        min_start = min(s[1] for s in segments)
        max_end = max(s[2] for s in segments)

        from sqlalchemy import bindparam

        session = self.db.get_session()
        try:
            stmt = text(
                "SELECT code, date, net_flow, big_net, small_net "
                "FROM stock_fund_flow "
                "WHERE code IN :codes AND date >= :s AND date <= :e"
            ).bindparams(bindparam("codes", expanding=True))
            rows = session.execute(stmt, {
                "codes": all_codes,
                "s": min_start,
                "e": max_end,
            }).fetchall()
        finally:
            session.close()

        # 按 code 分组缓存
        cache: Dict[str, List[Tuple]] = {}
        for r in rows:
            cache.setdefault(r.code, []).append((
                str(r.date), r.net_flow, r.big_net, r.small_net,
            ))

        # 按每只股票的横盘段日期范围聚合
        for code, start, end in segments:
            rows_c = cache.get(code, [])
            total, big, small = 0.0, 0.0, 0.0
            has_data = False
            for date_str, net_flow, big_net, small_net in rows_c:
                if not (start <= date_str <= end):
                    continue
                has_data = True
                if net_flow is not None:
                    total += float(net_flow)
                if big_net is not None:
                    big += float(big_net)
                if small_net is not None:
                    small += float(small_net)
            if has_data:
                result[code] = {
                    "total_net": round(total, 2),
                    "big_net": round(big, 2),
                    "small_net": round(small, 2),
                }
            else:
                result[code] = {"total_net": None, "big_net": None, "small_net": None}
        return result

    @staticmethod
    def _evaluate_fund_flow(
        total_net: Optional[float],
        big_net: Optional[float],
        small_net: Optional[float],
    ) -> str:
        """资金评价。"""
        if total_net is None or big_net is None or small_net is None:
            return "无数据"
        # 大资金流入 + 小资金流出 = 主力吸筹（最经典）
        if big_net > 0 and small_net < 0:
            return "主力吸筹"
        # 大资金流出 + 小资金流入 = 主力派发
        if big_net < 0 and small_net > 0:
            return "主力派发"
        # 同向：看总净流入方向
        if total_net > 0:
            return "净流入"
        if total_net < 0:
            return "净流出"
        return "中性"

    def _get_stock_name(self, code: str) -> str:
        if self._stock_name_provider:
            return self._stock_name_provider(code)
        try:
            from data_provider.base import get_data_fetcher_manager
            return get_data_fetcher_manager().get_stock_name(code) or ""
        except Exception:
            return ""


# ----------------------------------------------------------------------
# Format
# ----------------------------------------------------------------------

def format_sideways_report(candidates: List[SidewaysCandidate]) -> str:
    """格式化横盘选股报告（表格输出，按横盘长度倒序）。"""
    from tabulate import tabulate

    lines = [f"=== 横盘选股结果: {len(candidates)} 只 ===", ""]
    if not candidates:
        lines.append("(无)")
        return "\n".join(lines)

    detail = []
    for c in candidates:
        # 横盘范围：起止日期 + 天数
        range_str = f"{c.sideways_start_date}~{c.sideways_end_date}({c.sideways_len}天)"
        # 资金流（万元 → 亿元换算显示，2 位小数）
        ff_total = f"{c.ff_total_net/10000:+.2f}亿" if c.ff_total_net is not None else "-"
        ff_big = f"{c.ff_big_net/10000:+.2f}亿" if c.ff_big_net is not None else "-"
        ff_small = f"{c.ff_small_net/10000:+.2f}亿" if c.ff_small_net is not None else "-"
        wash_str = str(c.wash_score) if c.wash_score > 0 else "-"
        detail.append([
            c.code,
            (c.name or "-")[:8],
            f"{c.close:.2f}",
            range_str,
            c.days_since_sideways,
            f"{c.seg_avg_amount_yi:.2f}",
            ff_total, ff_big, ff_small, c.ff_eval,
            wash_str,
        ])

    headers = [
        "代码", "名称", "收盘", "横盘范围", "距今天",
        "日均额(亿)", "总净额", "大单", "小单", "资金评价", "洗盘分",
    ]
    lines.append(tabulate(
        detail, headers=headers, tablefmt="grid",
        numalign='right', stralign='right',
    ))
    lines.append("")
    lines.append(
        "说明: 排序=距今天升序→大单降序→小单降序（越近、大单净流入越多越靠前）；"
        "资金=横盘段累计净额(亿元)；资金评价: 主力吸筹=大单净流入+小单净流出，"
        "主力派发=大单净流出+小单净流入；洗盘分=横盘段最后一日洗盘评分(0-17)；"
        "距今天=0 表示今天还在横盘。"
    )
    return "\n".join(lines)
