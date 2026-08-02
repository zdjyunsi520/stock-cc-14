# -*- coding: utf-8 -*-
"""压价吸筹异常时段侦测器（单股）。

定义：
- 单日触发 = big_net>0 AND small_net<0 AND pct_chg<3%
- 时段聚类 = 相邻触发日在原数据中"索引差 ≤ max_gap+1"算同一段
  （即中间最多 max_gap 个未触发日，仍视为持续吸筹）

用法：
    detector = AnomalyPeriodDetector()
    periods = detector.detect('688498', max_gap=2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


def _parse_date(d) -> Optional[date]:
    """容错解析：date 对象直接返回，字符串 parse YYYY-MM-DD，其他返回 None。"""
    if isinstance(d, date):
        return d
    if isinstance(d, str) and len(d) >= 10:
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


@dataclass
class AnomalyPeriod:
    """单股的一个异常时段。"""
    code: str
    start_date: date               # 时段起始日（首个触发日）
    end_date: date                 # 时段结束日（末个触发日）
    trigger_days: int              # 时段内触发日数
    skipped_days: int              # 时段内未触发日数（中间跳过的交易日）
    span_days: int                 # 时段跨度（自然日，含周末）
    total_big_net: float           # 时段内大单净流入累计（万）
    total_small_net: float         # 时段内小单净流入累计（万，应为负）
    avg_pct_chg: float             # 时段内平均当日涨跌幅%
    price_pct: float               # 时段起末收盘涨幅%（看是否压价）
    start_close: float
    end_close: float


class AnomalyPeriodDetector:
    """压价吸筹异常时段侦测。"""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def detect(self, code: str, max_gap: int = 2) -> List[AnomalyPeriod]:
        """侦测单股的异常时段。

        Args:
            code: 6位股票代码
            max_gap: 时段内允许的最大连续未触发天数（默认2，即"隔一两天"）

        Returns:
            AnomalyPeriod 列表，按时间升序
        """
        code = str(code).zfill(6)
        rows = self._load(code)
        if len(rows) < 3:
            return []

        # 单日触发 + 记录原索引
        triggered = [(i, r) for i, r in enumerate(rows) if self._is_trigger(r)]
        if not triggered:
            return []

        # 聚类：相邻触发索引差 ≤ max_gap+1 算同时段
        periods: List[AnomalyPeriod] = []
        current = [triggered[0]]
        for k in range(1, len(triggered)):
            prev_idx = triggered[k - 1][0]
            cur_idx = triggered[k][0]
            if cur_idx - prev_idx <= max_gap + 1:
                current.append(triggered[k])
            else:
                periods.append(self._build_period(code, current))
                current = [triggered[k]]
        periods.append(self._build_period(code, current))
        return periods

    @staticmethod
    def _is_trigger(r: dict) -> bool:
        """单日触发判定：压价吸筹。"""
        big = r.get("big_net") or 0
        small = r.get("small_net") or 0
        pct = r.get("pct_chg") or 0
        return big > 0 and small < 0 and pct < 3

    @staticmethod
    def _build_period(code: str, members: list) -> AnomalyPeriod:
        rows = [r for _, r in members]
        idxs = [i for i, _ in members]
        span_index = idxs[-1] - idxs[0] + 1
        skipped = span_index - len(rows)

        bigs = [r.get("big_net") or 0 for r in rows]
        smalls = [r.get("small_net") or 0 for r in rows]
        pcts = [r.get("pct_chg") or 0 for r in rows]

        start_close = rows[0].get("close") or 0
        end_close = rows[-1].get("close") or 0
        price_pct = ((end_close - start_close) / max(start_close, 0.01)) * 100 if start_close else 0

        start_date = _parse_date(rows[0].get("date"))
        end_date = _parse_date(rows[-1].get("date"))
        span_days = (end_date - start_date).days + 1 if start_date and end_date else 0

        return AnomalyPeriod(
            code=code,
            start_date=start_date,
            end_date=end_date,
            trigger_days=len(rows),
            skipped_days=skipped,
            span_days=span_days,
            total_big_net=round(sum(bigs), 0),
            total_small_net=round(sum(smalls), 0),
            avg_pct_chg=round(sum(pcts) / len(pcts), 2),
            price_pct=round(price_pct, 2),
            start_close=start_close,
            end_close=end_close,
        )

    def _load(self, code: str) -> List[dict]:
        """加载该股的全部历史资金流（按 date 升序）。

        用 ORM 查询，保证 date 字段被 SQLAlchemy 转为 datetime.date。
        """
        from src.storage import StockFundFlow
        session = self.db.get_session()
        try:
            rows = session.query(
                StockFundFlow.date, StockFundFlow.close, StockFundFlow.pct_chg,
                StockFundFlow.big_net, StockFundFlow.small_net,
            ).filter(StockFundFlow.code == code).order_by(StockFundFlow.date.asc()).all()
            return [
                {'date': r[0], 'close': r[1], 'pct_chg': r[2],
                 'big_net': r[3], 'small_net': r[4]}
                for r in rows
            ]
        finally:
            session.close()


def format_periods(code: str, name: str, periods: List[AnomalyPeriod]) -> str:
    """格式化输出异常时段列表。"""
    if not periods:
        return f"=== {code} {name} ===\n未发现压价吸筹异常时段"

    lines = [
        f"=== {code} {name} ===",
        f"发现 {len(periods)} 个异常时段:",
        "-" * 100,
        f"{'时段':<6} {'起止':<26} {'触发/跨度':>10} {'跳过':>4} {'大单累计':>12} {'小单累计':>12} {'平均涨跌':>8} {'区间涨幅':>8}",
        "-" * 100,
    ]
    for i, p in enumerate(periods, 1):
        range_str = f"{p.start_date} ~ {p.end_date}"
        trig_str = f"{p.trigger_days}/{p.span_days}天"
        lines.append(
            f"#{i:<5} {range_str:<26} {trig_str:>10} {p.skipped_days:>4} "
            f"{p.total_big_net:>+10.0f}万 {p.total_small_net:>+10.0f}万 "
            f"{p.avg_pct_chg:>+6.2f}% {p.price_pct:>+6.2f}%"
        )
    return "\n".join(lines)
