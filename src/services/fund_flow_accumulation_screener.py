# -*- coding: utf-8 -*-
"""资金吸筹选股器 — 量价背离识别。

核心定义（与用户对齐）：
- 最近 N 天内，每天涨幅 ≤ 上限（默认 +3%），下跌不限
- N 天累计资金净流入 > 0（不需要每天流入）
- 按 N 天累计资金净流入降序

实战语义：资金偷偷吸筹但股价被压住（典型量价背离）。

数据源：stock_fund_flow 表（同花顺个股资金流）。
与现有 MinuteAccumulationDetector / SidewaysScreener 完全独立。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pandas as pd
from sqlalchemy import bindparam, text

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board

logger = logging.getLogger(__name__)


@dataclass
class AccumulationCriteria:
    """吸筹选股参数。"""
    lookback_days: int = 5                 # 最近 N 天
    daily_rise_max_pct: float = 3.0        # 每日涨幅上限（≤ 3% 视为压价；下跌不限）
    min_avg_amount_yi: float = 1.0         # N 天日均成交额 ≥ 此值（亿元）
    max_candidates: int = 50               # 最多返回候选


@dataclass
class DailyFlow:
    """单日资金流明细。"""
    date: str
    close: float
    pct_chg: float
    net_flow: float                        # 总净流入（万元）
    big_net: float                         # 大单净流入（万元）
    mid_net: float                         # 中单净流入（万元）
    small_net: float                       # 小单净流入（万元）


@dataclass
class AccumulationCandidate:
    """吸筹候选股。"""
    code: str
    name: str = ""
    close: float = 0.0
    total_net_wan: float = 0.0             # N 天累计总净流入（万元）
    big_net_wan: float = 0.0               # N 天累计大单净流入（万元）
    small_net_wan: float = 0.0             # N 天累计小单净流入（万元）
    mid_net_wan: float = 0.0               # N 天累计中单净流入（万元）
    avg_amount_yi: float = 0.0             # N 天日均成交额（亿元）
    ff_eval: str = "-"                     # 资金评价
    daily_flows: List[DailyFlow] = field(default_factory=list)


class FundFlowAccumulationScreener:
    """资金吸筹选股（量价背离识别）。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._stock_name_provider = stock_name_provider

    def screen(
        self, criteria: Optional[AccumulationCriteria] = None,
    ) -> List[AccumulationCandidate]:
        """跑吸筹选股。"""
        criteria = criteria or AccumulationCriteria()

        # Step 1: 单次 SQL 拉取全市场最近 N 天资金流
        raw_df = self._load_recent_fund_flow(criteria.lookback_days)
        if raw_df.empty:
            logger.warning("[AccumScreen] stock_fund_flow 无数据")
            return []

        # Step 2: 按 code 分组，过滤 + 聚合
        candidates = self._filter_and_aggregate(raw_df, criteria)
        if not candidates:
            logger.info("[AccumScreen] 无符合条件的股票")
            return []

        # Step 3: 关联股票名称
        for c in candidates:
            c.name = self._get_stock_name(c.code)

        # Step 4: 排序（累计净流入降序，大单降序作 tie-breaker）
        candidates.sort(key=lambda c: (-c.total_net_wan, -c.big_net_wan))
        result = candidates[:criteria.max_candidates]

        logger.info("[AccumScreen] 筛选结果: %d 只", len(result))
        return result

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_recent_fund_flow(self, lookback_days: int) -> pd.DataFrame:
        """单次 SQL 拉取全市场最近 N 天的资金流（含 close/pct_chg/net_flow/大中小单/amount）。

        注：stock_fund_flow 表无 amount 字段，用 (total_inflow + total_outflow) 近似。
        """
        session = self.db.get_session()
        try:
            # 用子查询取每只股票最近 N 个交易日（按日期降序）
            sql = text("""
                SELECT code, date, close, pct_chg,
                       net_flow, big_net, mid_net, small_net,
                       (COALESCE(total_inflow, 0) + COALESCE(total_outflow, 0)) AS amount
                FROM stock_fund_flow
                WHERE date >= (
                    SELECT MIN(d) FROM (
                        SELECT DISTINCT date AS d
                        FROM stock_fund_flow
                        ORDER BY date DESC
                        LIMIT :n
                    )
                )
            """)
            rows = session.execute(sql, {"n": lookback_days}).fetchall()
        finally:
            session.close()

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(
            rows, columns=[
                "code", "date", "close", "pct_chg",
                "net_flow", "big_net", "mid_net", "small_net", "amount",
            ],
        )

    # ------------------------------------------------------------------
    # Filter & Aggregate
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_and_aggregate(
        df: pd.DataFrame, criteria: AccumulationCriteria,
    ) -> List[AccumulationCandidate]:
        """按 code 分组：过滤 + 累计聚合。"""
        candidates: List[AccumulationCandidate] = []

        for code, group in df.groupby("code"):
            # 排除创业板/科创板/北交所
            if is_excluded_board(code):
                continue

            g = group.sort_values("date")
            # 必须有最近 lookback_days 的完整数据
            if len(g) < criteria.lookback_days:
                continue

            g = g.tail(criteria.lookback_days)

            # 维度 1：每天涨幅 ≤ daily_rise_max_pct（下跌不限）
            if (g["pct_chg"] > criteria.daily_rise_max_pct).any():
                continue

            # 维度 2：N 天累计资金净流入 > 0
            total_net = float(g["net_flow"].fillna(0).sum())
            if total_net <= 0:
                continue

            # 维度 3：日均成交额 ≥ min_avg_amount_yi
            # stock_fund_flow.total_inflow 单位是万元 → 亿元需 ÷ 1e4
            avg_amount_yi = float(g["amount"].fillna(0).mean()) / 1e4
            if avg_amount_yi < criteria.min_avg_amount_yi:
                continue

            big_net = float(g["big_net"].fillna(0).sum())
            mid_net = float(g["mid_net"].fillna(0).sum())
            small_net = float(g["small_net"].fillna(0).sum())

            # 每日明细（按日期升序）
            daily: List[DailyFlow] = []
            for _, r in g.iterrows():
                daily.append(DailyFlow(
                    date=str(r["date"]),
                    close=float(r["close"] or 0),
                    pct_chg=float(r["pct_chg"] or 0),
                    net_flow=float(r["net_flow"] or 0),
                    big_net=float(r["big_net"] or 0),
                    mid_net=float(r["mid_net"] or 0),
                    small_net=float(r["small_net"] or 0),
                ))

            candidates.append(AccumulationCandidate(
                code=code,
                close=round(float(g.iloc[-1]["close"] or 0), 2),
                total_net_wan=round(total_net, 2),
                big_net_wan=round(big_net, 2),
                mid_net_wan=round(mid_net, 2),
                small_net_wan=round(small_net, 2),
                avg_amount_yi=round(avg_amount_yi, 3),
                ff_eval=FundFlowAccumulationScreener._evaluate_fund_flow(
                    total_net, big_net, small_net,
                ),
                daily_flows=daily,
            ))
        return candidates

    @staticmethod
    def _evaluate_fund_flow(
        total_net: float, big_net: float, small_net: float,
    ) -> str:
        """资金评价。"""
        if big_net > 0 and small_net < 0:
            return "主力吸筹"
        if big_net < 0 and small_net > 0:
            return "主力派发"
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

def format_accumulation_report(
    candidates: List[AccumulationCandidate],
    lookback_days: int,
    daily_rise_max_pct: float,
) -> str:
    """格式化吸筹选股报告：汇总表 + 每日明细。"""
    from tabulate import tabulate

    lines = [
        f"=== 资金吸筹选股结果: {len(candidates)} 只 ===",
        f"条件: 最近 {lookback_days} 天 | 每日涨幅 ≤ +{daily_rise_max_pct:.1f}% (下跌不限)"
        f" | 累计资金净流入 > 0",
        "",
    ]
    if not candidates:
        lines.append("(无)")
        return "\n".join(lines)

    # 汇总表
    summary = []
    for c in candidates:
        summary.append([
            c.code,
            (c.name or "-")[:8],
            f"{c.close:.2f}",
            f"{c.total_net_wan/10000:+.2f}",
            f"{c.big_net_wan/10000:+.2f}",
            f"{c.mid_net_wan/10000:+.2f}",
            f"{c.small_net_wan/10000:+.2f}",
            f"{c.avg_amount_yi:.2f}",
            c.ff_eval,
        ])
    summary_headers = [
        "代码", "名称", "收盘",
        "累计净额(亿)", "大单(亿)", "中单(亿)", "小单(亿)",
        "日均额(亿)", "评价",
    ]
    lines.append("--- 汇总（按累计净额降序） ---")
    lines.append(tabulate(
        summary, headers=summary_headers, tablefmt="grid",
        numalign='right', stralign='right',
    ))
    lines.append("")

    # 每日明细
    lines.append("--- 每日明细（万元） ---")
    for c in candidates:
        title = f"[{c.code}] {(c.name or '-')[:8]}  收盘 {c.close:.2f}  累计净额 {c.total_net_wan/10000:+.2f}亿  评价: {c.ff_eval}"
        lines.append("")
        lines.append(title)

        detail = []
        for d in c.daily_flows:
            detail.append([
                d.date,
                f"{d.close:.2f}",
                f"{d.pct_chg:+.2f}%",
                f"{d.net_flow:+.0f}",
                f"{d.big_net:+.0f}",
                f"{d.mid_net:+.0f}",
                f"{d.small_net:+.0f}",
            ])
        detail_headers = [
            "日期", "收盘", "涨跌", "总净额", "大单", "中单", "小单",
        ]
        lines.append(tabulate(
            detail, headers=detail_headers, tablefmt="grid",
            numalign='right', stralign='right',
        ))

    return "\n".join(lines)
