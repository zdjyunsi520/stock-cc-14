# -*- coding: utf-8 -*-
"""主升浪选股器 — 基于"低量吸筹洗盘 → 高量主升浪"的量能形态识别。

核心定义（与用户对齐 v5，2026-06-28：去除极端值剔除步骤）：
1. 取最近 `lookback_days` 天（默认 20 天）作为分析窗口。
2. 在全窗口直接找 H（最大量）和 L（最小量），不剔除任何极端值。
3. **定律**：L 必须在 H 之前（l_idx < h_idx），先洗盘后拉升。
4. **低量组扩展**（吸筹洗盘）：以 L 为中心向两侧扩展，
   向左连续扩展；向右允许 ≤ `surge_max_break_days`（默认 2）天的 vol > L × `low_ratio` 断点
   被后续低位日桥接（断点也计入低量组区间），**低量组尾部 low_end 必须落在真正的低位日**。
5. **高量组扩展**（主升浪）：[low_end+1, H] 之内为基础，再从 H 当天向右扩展，
   volume ≥ H × `surge_extend_ratio`（默认 0.5）继续纳入；
   中途遇到 vol < 阈值的"断点"允许 ≤ `surge_max_break_days` 天桥接（断点也计入高量组）；
   断点 > 2 天或后续无新高量日时停止扩展。
6. **筛选门槛（后置）**：高量组平均量 / 低量组平均量 ≥ `h_l_ratio_min`（默认 1.9），
   否则视为不符合"洗盘→拉升"形态，跳过。
7. 排除 ST/*ST/退市、创业板/科创板/北交所（300/301/688/4/8 开头）。
8. **排序**：三层排序——
   (1) 主升浪天数升序（越短=越刚启动越优先）；
   (2) 30 日高位比例升序（`close / max(high in 30d)`，值越小=股价越处于低位越优先）；
   (3) 同前两者时按 (高量组平均量 / 低量组平均量) 倍率降序 —— 倍率越大洗盘越彻底、主升越猛。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

import numpy as np
import pandas as pd

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board, is_excluded_by_name

logger = logging.getLogger(__name__)


@dataclass
class SurgeCriteria:
    """主升浪选股判定参数。"""
    lookback_days: int = 20                 # 分析窗口（默认最近 20 天）
    # —— H/L 门槛（H/L = 高量组均量 ÷ 低量组均量）——
    h_l_ratio_min: float = 1.9              # 筛选门槛：均值比 ≥ 1.9 才入选
    # —— 低量组扩展（吸筹洗盘） ——
    low_ratio: float = 1.9                  # 低量组阈值：volume ≤ L × 1.9 视为低量
    min_low_group_len: int = 3              # 低量组至少 N 天（避免单日孤点）
    # —— 高量组扩展（主升浪） ——
    surge_extend_ratio: float = 0.5         # 高量组向后扩展阈值：vol ≥ H × 0.5 继续纳入
    surge_max_break_days: int = 2           # 断点容忍：≤2 天 vol<H/2 可被后续高量日桥接
    min_surge_group_len: int = 1            # 高量组至少 N 天（默认 1：H 当日就算主升浪）
    # —— 历史低位衡量（排序辅助） ——
    low_price_window_days: int = 30         # 用最近 N 天最高价衡量"历史低位"
    # —— 流动性门槛 ——
    min_avg_amount_yi: float = 1.0          # 全窗口日均成交额下限（亿元，过滤僵尸股）
    min_h_amount_yi: float = 3.0            # H 日成交额下限（亿元，保证主升浪有量）
    max_candidates: int = 50                # 最多返回候选数
    codes_whitelist: Optional[Set[str]] = field(default=None)  # 指定股票池：非空时只扫这些票，且跳过板块黑名单


@dataclass
class SurgeCandidate:
    """主升浪候选股。"""
    code: str
    name: str = ""
    close: float = 0.0                       # 窗口最后一日收盘价
    # H 日（量能最高日）
    h_date: str = ""                         # YYYY-MM-DD
    h_volume: int = 0                        # H 日成交量（手）
    h_amount_yi: float = 0.0                 # H 日成交额（亿元）
    h_pct_chg: float = 0.0                   # H 日涨跌幅%
    # L 日（量能最低日）
    l_date: str = ""
    l_volume: int = 0
    l_amount_yi: float = 0.0
    # 量比
    surge_low_avg_ratio: float = 0.0         # 高量组平均量 / 低量组平均量（既是门槛 ≥1.9 也是排序键 3）
    price_to_high_30: float = 0.0            # close / max(high in 30d)，值越小越处于低位（排序键 2）
    # 低量组（吸筹洗盘）
    low_start_date: str = ""
    low_end_date: str = ""
    low_group_len: int = 0
    low_avg_volume: int = 0
    low_avg_amount_yi: float = 0.0
    # 高量组（主升浪）
    surge_start_date: str = ""
    surge_end_date: str = ""
    surge_group_len: int = 0
    surge_avg_volume: int = 0
    surge_avg_amount_yi: float = 0.0
    surge_pct_chg: float = 0.0               # 主升浪阶段累计涨幅%
    # T+N 涨跌（end_date 之后的 close 对比，由 main.py 在选股后 enrich）
    returns: Dict[int, Optional[float]] = field(default_factory=dict)


class SurgeScreener:
    """主升浪选股。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
        end_date: Optional[str] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._stock_name_provider = stock_name_provider
        self._end_date = end_date

    def screen(self, criteria: Optional[SurgeCriteria] = None) -> List[SurgeCandidate]:
        """跑主升浪选股。"""
        criteria = criteria or SurgeCriteria()

        # Step 1: 批量加载全市场日线（带 buffer，且至少满足 30 天历史高位衡量）
        load_days = max(criteria.lookback_days, criteria.low_price_window_days) + 5
        df = self.db.get_bulk_daily_data(days=load_days, end_date=self._end_date)
        if df.empty:
            logger.warning("[SurgeScreen] 无日线数据")
            return []

        # Step 2: 按股票分组扫描
        metrics_df = self._compute_surge_metrics(df, criteria)
        if metrics_df.empty:
            logger.info("[SurgeScreen] 指标计算后为空")
            return []

        # Step 3: 过滤（量能 + 长度 + 定律）
        filtered = self._filter_surge(metrics_df, criteria)
        if filtered.empty:
            logger.info("[SurgeScreen] 无符合条件的股票")
            return []

        # Step 4: 构建候选 + 排序（H 量能降序）
        candidates = self._build_candidates(filtered, criteria)

        logger.info("[SurgeScreen] 筛选结果: %d 只", len(candidates))
        return candidates

    # ------------------------------------------------------------------
    # Surge-form filter (供 --wash-v4 等其他选股策略复用)
    # ------------------------------------------------------------------

    def filter_codes_by_surge(
        self,
        codes: Set[str],
        criteria: Optional[SurgeCriteria] = None,
    ) -> Set[str]:
        """判定给定股票池中哪些符合主升浪形态，返回通过过滤的 codes 集合。

        用法：在 wash 等其他策略筛选出的候选池上，硬过滤掉不符合"低量吸筹→高量主升"
        形态的股票。

        Args:
            codes: 候选股票池（白名单模式，跳过板块黑名单）
            criteria: 主升浪判定参数（默认 SurgeCriteria）

        Returns:
            通过主升浪过滤的 codes 子集
        """
        if not codes:
            return set()

        criteria = criteria or SurgeCriteria()
        criteria.codes_whitelist = set(codes)  # 白名单模式（避免改动入参）

        load_days = max(criteria.lookback_days, criteria.low_price_window_days) + 5
        df = self.db.get_bulk_daily_data(days=load_days, end_date=self._end_date)
        if df.empty:
            logger.warning("[SurgeFilter] 无日线数据，跳过过滤")
            return set()

        metrics_df = self._compute_surge_metrics(df, criteria)
        if metrics_df.empty:
            return set()

        filtered = self._filter_surge(metrics_df, criteria)
        valid_codes = set(filtered["code"].tolist())

        logger.info(
            "[SurgeFilter] 主升浪过滤: 输入 %d 只 → 通过 %d 只 → 剔除 %d 只",
            len(codes), len(valid_codes), len(codes) - len(valid_codes),
        )
        return valid_codes

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_surge_metrics(
        self, df: pd.DataFrame, criteria: SurgeCriteria,
    ) -> pd.DataFrame:
        """按 code 分组：极端值剔除 → 找 L/H → 扩展低/高量组 → 计算均值比率。"""
        results = []
        lookback = criteria.lookback_days
        whitelist = criteria.codes_whitelist

        for code, group in df.groupby("code"):
            # 白名单优先：非空时只扫白名单内的票，并跳过板块黑名单
            if whitelist is not None:
                if code not in whitelist:
                    continue
            else:
                # 排除创业板/科创板/北交所
                if is_excluded_board(code):
                    continue

            # 排除 ST/*ST/退市（白名单模式下也排除，避免用户误传）
            name = self._get_stock_name(code)
            if is_excluded_by_name(name):
                continue

            g = group.sort_values("date").copy()
            if len(g) < 5:
                continue

            # 取最近 lookback_days 天作为分析窗口
            window = g.tail(lookback) if len(g) >= lookback else g

            # 额外取最近 low_price_window_days 天衡量"历史低位"
            lpwd = criteria.low_price_window_days
            window_lp = g.tail(lpwd) if len(g) >= lpwd else g
            high_lp = float(window_lp["high"].max()) if "high" in window_lp.columns and len(window_lp) > 0 else 0.0
            close_now = float(window["close"].iloc[-1]) if len(window) > 0 else 0.0
            price_to_high_lp = round(close_now / high_lp, 4) if high_lp > 0 else 1.0
            # 值越小代表越处于低位

            volumes = window["volume"].to_numpy(dtype=float)
            amounts = window["amount"].to_numpy(dtype=float) if "amount" in window.columns else np.zeros_like(volumes)
            pct_chgs = window["pct_chg"].to_numpy(dtype=float) if "pct_chg" in window.columns else np.zeros_like(volumes)
            dates_raw = window["date"].to_numpy()
            dates = pd.to_datetime(dates_raw).strftime("%Y-%m-%d").to_numpy()
            closes = window["close"].to_numpy(dtype=float)
            n = len(volumes)
            if n < 5:
                continue

            # —— Step 1: 在全窗口直接找 L/H（不去除极端值）——
            l_idx = int(np.argmin(volumes))
            h_idx = int(np.argmax(volumes))

            # —— Step 2: 定律——L 必须在 H 之前（先洗盘后拉升）——
            if l_idx >= h_idx:
                continue

            l_vol = float(volumes[l_idx])
            h_vol = float(volumes[h_idx])

            # 注：H/L 门槛已后移至 Step 7（用均值比判断），此处不再做单点比前置过滤

            # —— Step 5: 低量组扩展（向左连续 + 向右断点 ≤max_break_days 桥接）——
            # 与高量组扩展对称；尾部 low_end 必须落在真正的低位日（"最后一天低位"约束）
            threshold_low = l_vol * criteria.low_ratio
            low_start = l_idx
            # 向左连续扩展（保持原逻辑）
            while low_start - 1 >= 0 and volumes[low_start - 1] <= threshold_low:
                low_start -= 1

            # 向右扩展：允许 ≤max_break_days 天 vol>阈值 的断点被后续低位日桥接（断点也纳入区间）
            low_end = l_idx
            pending_breaks_low: List[int] = []
            j = l_idx + 1
            while j < h_idx:  # 不能越过 H
                if volumes[j] <= threshold_low:
                    # 低位日：桥接成功，断点一并纳入 [low_start, low_end]
                    low_end = j
                    pending_breaks_low = []
                    j += 1
                else:
                    pending_breaks_low.append(j)
                    if len(pending_breaks_low) > criteria.surge_max_break_days:
                        # 断点超过容忍天数，停止扩展（断点不纳入）
                        break
                    j += 1
                    # 若到末尾或 H 之前再无低位日，pending_breaks_low 不被纳入（尾部低位约束）
            low_group_len = low_end - low_start + 1

            # —— Step 6: 高量组扩展（H 之后向右，vol ≥ H/2 继续；断点 ≤2 天桥接）——
            threshold_h_extend = h_vol * criteria.surge_extend_ratio
            surge_start = low_end + 1
            surge_end = h_idx
            pending_breaks: List[int] = []
            j = h_idx + 1
            while j < n:
                if volumes[j] >= threshold_h_extend:
                    # 高量日：把暂存的断点一并纳入高量组（含断点）
                    surge_end = j
                    pending_breaks = []
                    j += 1
                else:
                    # 低量日：作为断点暂存
                    pending_breaks.append(j)
                    if len(pending_breaks) > criteria.surge_max_break_days:
                        # 断点超过容忍天数，停止扩展（断点不纳入）
                        break
                    j += 1
                    # 若 j 到末尾，pending_breaks 因无后续高量日桥接而被舍弃
            surge_group_len = surge_end - surge_start + 1

            # —— Step 7: 计算均值比率（H/L = 高量组均量 ÷ 低量组均量）——
            low_volumes_slice = volumes[low_start: low_end + 1]
            surge_volumes_slice = volumes[surge_start: surge_end + 1]
            low_avg_volume = float(low_volumes_slice.mean()) if len(low_volumes_slice) > 0 else 0.0
            surge_avg_volume = float(surge_volumes_slice.mean()) if len(surge_volumes_slice) > 0 else 0.0
            surge_low_ratio = round(surge_avg_volume / low_avg_volume, 2) if low_avg_volume > 0 else 0.0

            # —— Step 8: 门槛（后置）——H/L 均值比 ≥ h_l_ratio_min（默认 1.9）——
            # 注：单点 H_vol/L_vol 已不再作前置过滤（用户对齐：H/L 应为均值比）
            if low_avg_volume <= 0 or surge_low_ratio < criteria.h_l_ratio_min:
                continue

            # 全窗口日均成交额（亿元）
            valid_amounts = [a for a in amounts if a and a > 0]
            avg_amount_yi = (sum(valid_amounts) / len(valid_amounts) / 1e8) if valid_amounts else 0.0

            # 低量组日均额
            low_amounts = amounts[low_start: low_end + 1]
            low_valid_amt = [a for a in low_amounts if a and a > 0]
            low_avg_amount_yi = (
                sum(low_valid_amt) / len(low_valid_amt) / 1e8 if low_valid_amt else 0.0
            )

            # 高量组日均额 / 累计涨幅
            surge_amounts = amounts[surge_start: surge_end + 1]
            surge_valid_amt = [a for a in surge_amounts if a and a > 0]
            surge_avg_amount_yi = (
                sum(surge_valid_amt) / len(surge_valid_amt) / 1e8 if surge_valid_amt else 0.0
            )
            surge_pct_chg = 0.0
            if surge_end > surge_start and closes[surge_start] > 0:
                surge_pct_chg = (closes[surge_end] - closes[surge_start]) / closes[surge_start] * 100
            elif len(pct_chgs[surge_start: surge_end + 1]) > 0:
                surge_pct_chg = float(np.sum(pct_chgs[surge_start: surge_end + 1]))

            results.append({
                "code": code,
                "name": name,
                "close": float(closes[-1]),
                "price_to_high_lp": price_to_high_lp,
                "h_idx": h_idx,
                "l_idx": l_idx,
                "h_date": str(dates[h_idx]),
                "h_volume": int(h_vol),
                "h_amount_yi": round(float(amounts[h_idx]) / 1e8, 3) if amounts[h_idx] else 0.0,
                "h_pct_chg": round(float(pct_chgs[h_idx]), 2),
                "l_date": str(dates[l_idx]),
                "l_volume": int(l_vol),
                "l_amount_yi": round(float(amounts[l_idx]) / 1e8, 3) if amounts[l_idx] else 0.0,
                "surge_low_avg_ratio": surge_low_ratio,
                "low_start_date": str(dates[low_start]),
                "low_end_date": str(dates[low_end]),
                "low_group_len": int(low_group_len),
                "low_avg_volume": int(low_avg_volume),
                "low_avg_amount_yi": round(low_avg_amount_yi, 3),
                "surge_start_date": str(dates[surge_start]),
                "surge_end_date": str(dates[surge_end]),
                "surge_group_len": int(surge_group_len),
                "surge_avg_volume": int(surge_avg_volume),
                "surge_avg_amount_yi": round(surge_avg_amount_yi, 3),
                "surge_pct_chg": round(surge_pct_chg, 2),
                "avg_amount_yi": round(avg_amount_yi, 3),
            })

        if not results:
            return pd.DataFrame()
        return pd.DataFrame(results)

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_surge(df: pd.DataFrame, criteria: SurgeCriteria) -> pd.DataFrame:
        """保留符合量能门槛与长度门槛的股票。

        注：H/L（=surge_low_avg_ratio）≥ 1.9 的核心门槛已在 metrics 计算阶段拦截（避免无效扩展），
        此处再次校验防止外部修改 criteria 后产生不一致。
        """
        df = df.dropna(subset=["h_volume", "l_volume", "avg_amount_yi"])
        mask = (
            (df["low_group_len"] >= criteria.min_low_group_len)
            & (df["surge_group_len"] >= criteria.min_surge_group_len)
            & (df["avg_amount_yi"] >= criteria.min_avg_amount_yi)
            & (df["h_amount_yi"] >= criteria.min_h_amount_yi)
            & (df["surge_low_avg_ratio"] >= criteria.h_l_ratio_min)  # 均值比门槛 ≥ 1.9
        )
        return df[mask].copy()

    # ------------------------------------------------------------------
    # Build candidates
    # ------------------------------------------------------------------

    def _build_candidates(
        self, filtered: pd.DataFrame, criteria: SurgeCriteria,
    ) -> List[SurgeCandidate]:
        """构建候选、按 (高量组均值 / 低量组均值) 倍率降序输出。"""
        candidates: List[SurgeCandidate] = []
        for _, row in filtered.iterrows():
            code = row["code"]
            name = row.get("name") or self._get_stock_name(code)
            candidates.append(SurgeCandidate(
                code=code,
                name=name,
                close=round(float(row["close"]), 2),
                h_date=str(row["h_date"]),
                h_volume=int(row["h_volume"]),
                h_amount_yi=round(float(row["h_amount_yi"]), 3),
                h_pct_chg=round(float(row["h_pct_chg"]), 2),
                l_date=str(row["l_date"]),
                l_volume=int(row["l_volume"]),
                l_amount_yi=round(float(row["l_amount_yi"]), 3),
                surge_low_avg_ratio=round(float(row["surge_low_avg_ratio"]), 2),
                price_to_high_30=round(float(row["price_to_high_lp"]), 4),
                low_start_date=str(row["low_start_date"]),
                low_end_date=str(row["low_end_date"]),
                low_group_len=int(row["low_group_len"]),
                low_avg_volume=int(row["low_avg_volume"]),
                low_avg_amount_yi=round(float(row["low_avg_amount_yi"]), 3),
                surge_start_date=str(row["surge_start_date"]),
                surge_end_date=str(row["surge_end_date"]),
                surge_group_len=int(row["surge_group_len"]),
                surge_avg_volume=int(row["surge_avg_volume"]),
                surge_avg_amount_yi=round(float(row["surge_avg_amount_yi"]), 3),
                surge_pct_chg=round(float(row["surge_pct_chg"]), 2),
            ))

        # 排序：主升浪天数升序 → 历史低位比例升序（越小越优先）→ 同前两者时倍率降序
        candidates.sort(key=lambda c: (c.surge_group_len, c.price_to_high_30, -c.surge_low_avg_ratio))
        return candidates[:criteria.max_candidates]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

def format_surge_report(
    candidates: List[SurgeCandidate],
    *,
    codes_whitelist: Optional[Set[str]] = None,
) -> str:
    """格式化主升浪选股报告（表格输出，按 倍率=surge_avg/low_avg 降序）。"""
    from tabulate import tabulate

    lines = [f"=== 主升浪选股结果: {len(candidates)} 只 ===", ""]
    if not candidates:
        lines.append("(无)")
        return "\n".join(lines)

    detail = []
    for c in candidates:
        low_range = f"{c.low_start_date}~{c.low_end_date}({c.low_group_len}天)"
        surge_range = f"{c.surge_start_date}~{c.surge_end_date}({c.surge_group_len}天)"
        row = [
            c.code,
            (c.name or "-")[:8],
            f"{c.close:.2f}",
            f"{c.price_to_high_30*100:.1f}%",   # 30日高位比例（越低越优先）
            f"{c.surge_low_avg_ratio:.1f}",     # H/L = 均值比（门槛≥1.9，排序键 3）
            low_range,
            surge_range,
        ]
        for d in (1, 2, 3, 4, 5):
            v = c.returns.get(d) if c.returns else None
            row.append(f"{v:+.2f}%" if v is not None else "-")
        detail.append(row)

    headers = [
        "代码", "名称", "收盘",
        "30日高位", "H/L",
        "吸筹洗盘范围", "主升浪范围",
        "T+1", "T+2", "T+3", "T+4", "T+5",
    ]
    lines.append(tabulate(
        detail, headers=headers, tablefmt='grid',
        numalign='right', stralign='right',
    ))
    lines.append("")
    if codes_whitelist is not None:
        scope_note = f"股票池模式: 仅扫描指定的 {len(codes_whitelist)} 只股票（含科创板/创业板），仅排除 ST/退市"
    else:
        scope_note = "排除创业板/科创板/北交所/ST/退市"
    lines.append(
        f"说明: 排序=主升浪天数升序→30日高位比例升序→H/L降序（主升越短越刚启动、股价越低位、均值比越大越彻底）；"
        f"30日高位=现价÷最近30天最高价（值越小越处于低位）；"
        f"H/L=高量组均量÷低量组均量(门槛≥1.9，扩展后判断)；"
        f"T+1~T+5=窗口最后一日(close)之后连续5个交易日的累计涨跌(close_N÷close_T - 1，未到日期显示-)；"
        f"算法: 全窗口直接找L/H(不去极端值)→L*1.9向左右扩低量组→H之后向右扩高量组(vol≥H/2继续,断点≤2天含桥接)→算均值比；"
        f"{scope_note}。"
    )
    return "\n".join(lines)
