# -*- coding: utf-8 -*-
"""洗盘选股回测器：复现 T 日的 --wash 买入/观察列表，统计 T+1/T+3/T+5 累计涨幅。

流程：
1. 调 PatternScreener + FundFlowScreener 复现 T 日 (buy_list, observe_list)
2. 从 stock_fund_flow 取每只股票 T 及之后的 close，算 T+N 涨幅
3. 输出 buy / observe 分组明细（含规律分/洗盘分/概念分/资金评价）+ 各窗口统计

用法（CLI）:
    python main.py --wash-backtest 20260608
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

HOLDING_DAYS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


@dataclass
class BacktestRow:
    """单只股票的回测行。"""
    code: str
    name: str
    grade: str                                       # 'buy' / 'observe' / 'avoid' / 'rejected_v3'
    close_t: float                                   # T 日收盘价
    returns: Dict[int, Optional[float]] = field(default_factory=dict)
    # 评分维度
    pattern_score: float = 0.0                       # 规律分合计
    wash_score: int = 0                              # 洗盘分（0-20）
    concept_count: int = 0                           # 命中热门题材数
    ff_matrix: str = ""                              # 资金结构 大↑小↓
    ff_prob_acc: float = 0.0                         # 吸筹概率%
    # 规律分明细（5 分项）
    sc_concept: float = 0.0                          # 概念分（hot_concepts）
    sc_vol: float = 0.0                              # 量比分（min(vol_ratio,3)×10）
    sc_up_days: float = 0.0                          # 上涨分（up_days_5×5）
    sc_ma10: float = 0.0                             # MA10 贴近分（0/10）
    sc_chase: float = 0.0                            # 追高罚分（0/-5）
    v3_final: Optional[float] = None                 # v3 综合分（None=未入选）


@dataclass
class BacktestSummary:
    """回测汇总。"""
    date_t: str
    rows: List[BacktestRow] = field(default_factory=list)
    stats_buy: Dict[int, Dict[str, Optional[float]]] = field(default_factory=dict)
    stats_observe: Dict[int, Dict[str, Optional[float]]] = field(default_factory=dict)
    stats_avoid: Dict[int, Dict[str, Optional[float]]] = field(default_factory=dict)
    stats_rejected_v3: Dict[int, Dict[str, Optional[float]]] = field(default_factory=dict)
    stats_rejected_surge: Dict[int, Dict[str, Optional[float]]] = field(default_factory=dict)


class WashBacktester:
    """洗盘选股回测。"""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def run(
        self,
        date_t: str,
        *,
        surge_filter: bool = False,
        surge_criteria=None,
    ) -> BacktestSummary:
        """跑 T 日的回测。

        Args:
            date_t: T 日，8 位 YYYYMMDD
            surge_filter: 是否启用主升浪形态过滤（V4 模式）
            surge_criteria: 主升浪判定参数（仅 surge_filter=True 时生效）
        """
        logger.info(
            "[WashBacktest] T=%s 开始 (surge_filter=%s)",
            date_t, surge_filter,
        )

        picks, rejected_v3, rejected_surge = self._get_wash_pick_list(
            date_t, surge_filter=surge_filter, surge_criteria=surge_criteria,
        )
        if not picks and not rejected_v3 and not rejected_surge:
            logger.warning("[WashBacktest] T=%s 无候选", date_t)
            return BacktestSummary(date_t=date_t)

        rows: List[BacktestRow] = []
        for verdict, candidate, grade in picks:
            row = self._compute_returns(verdict, candidate, grade, date_t)
            if row:
                rows.append(row)
        for verdict, candidate, grade in rejected_v3:
            row = self._compute_returns(verdict, candidate, grade, date_t)
            if row:
                rows.append(row)
        for verdict, candidate, grade in rejected_surge:
            row = self._compute_returns(verdict, candidate, grade, date_t)
            if row:
                rows.append(row)

        stats_buy = self._compute_stats([r for r in rows if r.grade == 'buy'])
        stats_observe = self._compute_stats([r for r in rows if r.grade == 'observe'])
        stats_avoid = self._compute_stats([r for r in rows if r.grade == 'avoid'])
        stats_rejected_v3 = self._compute_stats([r for r in rows if r.grade == 'rejected_v3'])
        stats_rejected_surge = self._compute_stats([r for r in rows if r.grade == 'rejected_surge'])

        n_buy = sum(1 for r in rows if r.grade == 'buy')
        n_obs = sum(1 for r in rows if r.grade == 'observe')
        n_avd = sum(1 for r in rows if r.grade == 'avoid')
        n_rej = sum(1 for r in rows if r.grade == 'rejected_v3')
        n_rej_surge = sum(1 for r in rows if r.grade == 'rejected_surge')
        logger.info(
            "[WashBacktest] T=%s 完成: 买入=%d 观察=%d 回避=%d v3淘汰=%d surge淘汰=%d",
            date_t, n_buy, n_obs, n_avd, n_rej, n_rej_surge,
        )
        return BacktestSummary(
            date_t=date_t, rows=rows,
            stats_buy=stats_buy, stats_observe=stats_observe,
            stats_avoid=stats_avoid, stats_rejected_v3=stats_rejected_v3,
            stats_rejected_surge=stats_rejected_surge,
        )

    def _get_wash_pick_list(
        self,
        date_t: str,
        *,
        surge_filter: bool = False,
        surge_criteria=None,
    ) -> Tuple[List[Tuple], List[Tuple], List[Tuple]]:
        """复用 PatternScreener + FundFlowScreener 复现 T 日的 (verdict, candidate, grade) 列表。

        Returns:
            (picks, rejected_v3, rejected_surge)
            picks: 进入资金流筛选的 (verdict, candidate, grade) — grade ∈ {buy, observe, avoid}
            rejected_v3: v3 淘汰的 (None, candidate, 'rejected_v3') — 基础初筛过但 score/wash 不达 v3 阈值
            rejected_surge: surge 淘汰的 (None, candidate, 'rejected_surge') — V4 模式下未通过主升浪形态
        """
        from src.services.pattern_screener import (
            PatternScreener, PatternScreenerConfig,
        )
        from src.services.fund_flow_screener import FundFlowScreener

        screener = PatternScreener()
        config = PatternScreenerConfig()
        # force_refresh=True：绕过 pattern_cache，候选评分实时计算
        # concept_cache（题材成分股）仍由 _load_theme_universe 自动复用（数据源本就是历史快照）
        candidates, _ = screener.screen(config, date_key=date_t, force_refresh=True)
        if not candidates:
            return [], [], []

        picked = []
        rejected_v3 = []
        for c in candidates:
            f = PatternScreener.compute_wash_final(c.score, c.wash_score)
            if f is not None:
                c._wash_final = f
                picked.append(c)
            else:
                rejected_v3.append((None, c, 'rejected_v3'))
        picked.sort(key=lambda c: -c._wash_final)

        # V4: 主升浪形态过滤（在资金流筛选之前剔除不符合"低量吸筹→高量主升"形态的股票）
        rejected_surge: List[Tuple] = []
        if surge_filter and picked:
            from src.services.surge_screener import SurgeScreener

            surge_screener = SurgeScreener(end_date=date_t)
            valid_codes = surge_screener.filter_codes_by_surge(
                codes={c.code for c in picked},
                criteria=surge_criteria,
            )
            new_picked = []
            for c in picked:
                if c.code in valid_codes:
                    new_picked.append(c)
                else:
                    rejected_surge.append((None, c, 'rejected_surge'))
            picked = new_picked

        if not picked:
            return [], rejected_v3, rejected_surge

        ff = FundFlowScreener()
        buy_list, observe_list, avoid_list = ff.filter_candidates(picked, date_key=date_t)

        # 把 verdict 与原 candidate 配对（保留 pattern 维度的分数）
        cand_by_code = {c.code: c for c in picked}
        picks: List[Tuple] = []
        for v in buy_list:
            c = cand_by_code.get(v.code)
            if c:
                picks.append((v, c, 'buy'))
        for v in observe_list:
            c = cand_by_code.get(v.code)
            if c:
                picks.append((v, c, 'observe'))
        for v in avoid_list:
            c = cand_by_code.get(v.code)
            if c:
                picks.append((v, c, 'avoid'))
        return picks, rejected_v3, rejected_surge

    def _compute_returns(
        self, verdict, candidate, grade: str, date_t: str,
    ) -> Optional[BacktestRow]:
        """查 code 在 T 及之后的 close，算 T+1/T+3/T+5 累计涨幅，并携带评分维度。

        数据来源：stock_daily 日线表（baostock 同步，覆盖全市场）。
        不用 stock_fund_flow（资金流表，6-15 等近期日期入库不全）。
        """
        from src.storage import StockDaily
        try:
            t_date = datetime.strptime(date_t, "%Y%m%d").date()
        except ValueError:
            return None

        code = candidate.code
        session = self.db.get_session()
        try:
            rows = session.query(
                StockDaily.date, StockDaily.close,
            ).filter(
                StockDaily.code == code,
                StockDaily.date >= t_date,
            ).order_by(StockDaily.date.asc()).all()
        finally:
            session.close()

        if not rows:
            return None
        close_t = rows[0][1]
        if not close_t or close_t <= 0:
            return None

        # T+N 改为"和昨日对比的日间涨跌"（T+1 仍 vs T，T+2 vs T+1，依此类推）
        returns: Dict[int, Optional[float]] = {}
        prev_close = close_t
        for d in HOLDING_DAYS:
            if len(rows) > d:
                close_n = rows[d][1]
                if close_n and close_n > 0 and prev_close and prev_close > 0:
                    returns[d] = round((close_n - prev_close) / prev_close * 100, 2)
                    prev_close = close_n
                else:
                    returns[d] = None
            else:
                returns[d] = None

        # 规律分 5 分项重算（与 _score_and_filter 公式一致）
        sc_concept = float(candidate.theme_score)            # len(themes)×10
        sc_vol = min(float(candidate.vol_ratio or 0), 3.0) * 10
        sc_up_days = int(candidate.up_days_5 or 0) * 5
        sc_ma10 = 10 if 0 <= float(candidate.dist_ma10 or 0) <= 5 else 0
        sc_chase = -5 if float(candidate.ret5 or 0) > 10 else 0

        # v3 综合分（grade='rejected_v3' 时为 None）
        v3_final = getattr(candidate, '_wash_final', None)

        return BacktestRow(
            code=code,
            name=candidate.name or '',
            grade=grade,
            close_t=close_t,
            returns=returns,
            pattern_score=candidate.score,
            wash_score=candidate.wash_score,
            concept_count=len(candidate.themes or []),
            ff_matrix=getattr(verdict, 'matrix', '') if verdict else '',
            ff_prob_acc=getattr(verdict, 'prob_acc', 0.0) if verdict else 0.0,
            sc_concept=sc_concept,
            sc_vol=sc_vol,
            sc_up_days=sc_up_days,
            sc_ma10=sc_ma10,
            sc_chase=sc_chase,
            v3_final=v3_final,
        )

    @staticmethod
    def _compute_stats(rows: List[BacktestRow]) -> Dict[int, Dict[str, Optional[float]]]:
        """算各窗口的平均/胜率/中位数。"""
        stats: Dict[int, Dict[str, Optional[float]]] = {}
        total = len(rows)
        for d in HOLDING_DAYS:
            values = [r.returns.get(d) for r in rows if r.returns.get(d) is not None]
            if not values:
                stats[d] = {
                    'avg': None, 'win_rate': None, 'median': None,
                    'sample': 0, 'total': total,
                }
                continue
            avg = sum(values) / len(values)
            wins = sum(1 for v in values if v > 0)
            sorted_v = sorted(values)
            median = sorted_v[len(sorted_v) // 2]
            stats[d] = {
                'avg': round(avg, 2),
                'win_rate': round(wins / len(values) * 100, 1),
                'median': round(median, 2),
                'sample': len(values),
                'total': total,
            }
        return stats


def _format_pct(v: Optional[float]) -> str:
    return f"{v:+.2f}%" if v is not None else "N/A"


def _format_section(
    title: str, rows: List[BacktestRow],
    stats: Dict[int, Dict[str, Optional[float]]] = None,
) -> List[str]:
    """格式化某一档（buy/observe/avoid/rejected_v3）的明细表。"""
    from tabulate import tabulate

    lines: List[str] = [f"--- {title}: {len(rows)} 只 ---"]
    if not rows:
        lines.append("(无)")
        lines.append("")
        return lines

    detail = []
    for r in rows:
        row = [
            r.code,
            (r.name or '-')[:8],
            int(r.pattern_score),
            r.wash_score,
            r.concept_count,
            r.ff_matrix or '-',
            f"{r.ff_prob_acc:.0f}%" if r.ff_prob_acc else '-',
            f"{r.close_t:.2f}",
        ]
        for d in HOLDING_DAYS:
            row.append(_format_pct(r.returns.get(d)))
        detail.append(row)
    detail_headers = ['代码', '名称', '规律', '洗盘', '概念', '资金', '吸筹%', '收盘T'] \
                     + [f"T+{d}" for d in HOLDING_DAYS]
    lines.append(tabulate(
        detail, headers=detail_headers, tablefmt='grid', numalign='right', stralign='right',
    ))
    lines.append("")
    return lines


def format_backtest(summary: BacktestSummary) -> str:
    """格式化输出回测结果（买入/观察分组，含评分维度）。"""
    if not summary.rows:
        return f"=== 洗盘选股回测 T={summary.date_t} ===\n无买入/观察推荐或无资金流数据"

    buy_rows = [r for r in summary.rows if r.grade == 'buy']
    observe_rows = [r for r in summary.rows if r.grade == 'observe']
    avoid_rows = [r for r in summary.rows if r.grade == 'avoid']
    rejected_v3_rows = [r for r in summary.rows if r.grade == 'rejected_v3']
    rejected_surge_rows = [r for r in summary.rows if r.grade == 'rejected_surge']

    lines = [f"=== 洗盘选股回测 T={summary.date_t} ===", ""]
    lines.extend(_format_section("买入 buy", buy_rows))
    lines.extend(_format_section("观察 observe", observe_rows))
    lines.extend(_format_section("回避 avoid", avoid_rows))
    if rejected_surge_rows:
        lines.extend(_format_section(
            "主升浪淘汰 (规律+洗盘达标但未通过 V4 形态)", rejected_surge_rows,
        ))
    lines.extend(_format_section("v3淘汰 (基础初筛过但 score/wash 不达 v3 阈值)", rejected_v3_rows))
    lines.extend(_format_stats(summary))
    lines.extend(_format_score_breakdown(summary.rows))
    return "\n".join(lines)


def _format_stats(summary: BacktestSummary) -> List[str]:
    """各档位 T+N 平均/胜率/中位数对比（验证主升浪过滤是否有效）。"""
    from tabulate import tabulate

    groups = [
        ('买入 buy', summary.stats_buy),
        ('观察 observe', summary.stats_observe),
        ('回避 avoid', summary.stats_avoid),
    ]
    if summary.stats_rejected_surge:
        groups.append(('主升浪淘汰', summary.stats_rejected_surge))
    if summary.stats_rejected_v3:
        groups.append(('v3淘汰', summary.stats_rejected_v3))

    lines: List[str] = ["--- 各档位 T+N 统计 ---"]
    has_any = False
    for label, stats in groups:
        if not stats:
            continue
        has_any = True
        n_total = next((s.get('total') for s in stats.values() if s), 0)
        lines.append(f"[{label}] 共 {n_total} 只")
        rows = []
        for d in HOLDING_DAYS:
            s = stats.get(d, {})
            if not s or s.get('sample', 0) == 0:
                rows.append([f"T+{d}", '-', '-', '-', 0])
                continue
            rows.append([
                f"T+{d}",
                _format_pct(s.get('avg')),
                f"{s.get('win_rate'):.1f}%" if s.get('win_rate') is not None else '-',
                _format_pct(s.get('median')),
                s.get('sample', 0),
            ])
        lines.append(tabulate(
            rows, headers=['窗口', '平均', '胜率', '中位', '样本'],
            tablefmt='grid', numalign='right', stralign='right',
        ))
    if not has_any:
        lines.append("(无统计数据)")
    lines.append("")
    return lines


def _format_score_breakdown(rows: List[BacktestRow]) -> List[str]:
    """所有股票的规律分组成明细表（5 分项 + 洗盘 + v3）。"""
    from tabulate import tabulate

    lines: List[str] = [f"--- 分数组成明细: {len(rows)} 只 ---"]
    if not rows:
        lines.append("(无)")
        lines.append("")
        return lines

    grade_label = {
        'buy': '买入', 'observe': '观察',
        'avoid': '回避', 'rejected_v3': 'v3淘汰',
        'rejected_surge': '主升淘汰',
    }
    # 按 v3 综合分降序（None 排最后）
    sorted_rows = sorted(
        rows,
        key=lambda r: (r.v3_final is None, -(r.v3_final or 0)),
    )

    detail = []
    for r in sorted_rows:
        detail.append([
            grade_label.get(r.grade, r.grade),
            r.code,
            (r.name or '-')[:8],
            int(r.sc_concept),
            round(r.sc_vol, 1),
            int(r.sc_up_days),
            int(r.sc_ma10),
            int(r.sc_chase),
            round(r.pattern_score, 1),
            r.wash_score,
            f"{r.v3_final:.1f}" if r.v3_final is not None else '-',
        ])
    headers = ['档位', '代码', '名称', '概念', '量比', '上涨', 'MA10', '追高', '规律', '洗盘', 'v3']
    lines.append(tabulate(
        detail, headers=headers, tablefmt='grid', numalign='right', stralign='right',
    ))
    lines.append("")
    return lines
