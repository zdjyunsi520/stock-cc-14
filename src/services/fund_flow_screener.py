# -*- coding: utf-8 -*-
"""资金流洗盘过滤器。

基于"股票资金流状态识别模型"，对 PatternScreener 的洗盘选股结果做二次过滤。

四天回测验证的有效规则：
- 优质买入信号：价格位置<60% + 大单强度>0 + 命中"压盘吸筹"
  四天14只 → 14涨 (100%)
- 高风险回避：命中"高位派发"（价格位置>70% + 大单净流出 + 小单净流入）
  顶部预警准确率高

参考: 股票资金流状态识别模型.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class FundFlowVerdict:
    """单只股票的资金流判定。"""
    code: str
    name: str = ""
    last_close: float = 0         # 最新收盘价（用于推荐股数）
    # 资金流指标
    big_net_5: float = 0          # 5日大单净流入(万)
    small_net_5: float = 0        # 5日小单净流入(万)
    mid_net_5: float = 0          # 5日中单净流入(万)
    price_5_pct: float = 0        # 5日涨跌幅%
    price_position: float = 0     # 价格位置 0~1
    big_strength: float = 0       # 大单强度 -1~+1
    retail_strength: float = 0    # 散户强度 -1~+1
    # 结构判定
    matrix: str = ""              # 资金结构: 大↑小↓ 等
    states_hit: List[str] = field(default_factory=list)  # 命中的状态
    prob_acc: float = 0           # 吸筹概率%
    prob_dist: float = 0          # 派发概率%
    # 最终分类
    grade: str = "observe"        # buy / observe / avoid
    reason: str = ""              # 分类原因


# 单只股票目标仓位成本（元），用于推荐买入股数；高价股至少 1 手
TARGET_POSITION_VALUE_YUAN = 50000.0


def calc_lots(close: float, target_value: float = TARGET_POSITION_VALUE_YUAN) -> tuple:
    """根据股价与目标成本计算推荐手数。

    1 手 = 100 股；向下取整确保不超预算；最少 1 手（避免高价股返回 0）。

    Returns:
        (lots, actual_cost) — 手数 与 实际成本（元）
    """
    if close <= 0:
        return 0, 0.0
    lots = max(1, int(target_value // (close * 100)))
    return lots, lots * 100 * close


class FundFlowScreener:
    """资金流过滤器：对洗盘选股结果做资金结构过滤。"""

    # 阈值（基于四天回测）
    PRICE_POSITION_BUY = 0.6      # 优质买入的价格位置上限
    PRICE_POSITION_DANGER = 0.7   # 高位派发的价格位置下限
    BIG_STRENGTH_BUY = 0.0        # 优质买入的大单强度下限
    PRICE_5D_BUY_MAX = 8.0        # 优质买入的5日涨幅上限（避免追高获利盘）
    PROB_DIST_AVOID = 70.0        # 派发概率超过此值视为风险

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def filter_candidates(
        self,
        candidates: List,
        *,
        date_key: Optional[str] = None,
    ) -> Tuple[List[FundFlowVerdict], List[FundFlowVerdict], List[FundFlowVerdict]]:
        """对洗盘候选股做资金流过滤。

        Args:
            candidates: PatternScreener 输出的候选列表
            date_key: 截止日期 YYYYMMDD，None 取数据库最新

        Returns:
            (buy_list, observe_list, avoid_list)
        """
        end_date = self._normalize_date(date_key)
        buy_list, observe_list, avoid_list = [], [], []

        for c in candidates:
            code = getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else None)
            name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else "")
            if not code:
                continue

            verdict = self._analyze_one(code, name, end_date)
            if verdict is None:
                continue

            if verdict.grade == "buy":
                buy_list.append(verdict)
            elif verdict.grade == "avoid":
                avoid_list.append(verdict)
            else:
                observe_list.append(verdict)

        buy_list.sort(key=lambda v: (-v.prob_acc, v.price_position))
        avoid_list.sort(key=lambda v: -v.prob_dist)

        logger.info(
            "[FundFlowFilter] 过滤完成: 买入=%d 观察=%d 回避=%d (共%d只)",
            len(buy_list), len(observe_list), len(avoid_list),
            len(buy_list) + len(observe_list) + len(avoid_list),
        )
        return buy_list, observe_list, avoid_list

    # ------------------------------------------------------------------
    # 核心：单只股票资金流分析
    # ------------------------------------------------------------------

    def _analyze_one(self, code: str, name: str, end_date: Optional[date]) -> Optional[FundFlowVerdict]:
        rows = self._load_rows(code, end_date)
        if len(rows) < 10:
            logger.debug("[FundFlowFilter] %s 数据不足(%d天)", code, len(rows))
            return None

        n = len(rows)
        last = rows[-1]
        recent5 = rows[-5:]
        recent3 = rows[-3:]
        prev10 = rows[-13:-3] if n >= 13 else rows[:-3]

        big_net_5 = sum(r["big_net"] for r in recent5)
        small_net_5 = sum(r["small_net"] for r in recent5)
        mid_net_5 = sum(r["mid_net"] for r in recent5)
        price_5_pct = (recent5[-1]["close"] - recent5[0]["close"]) / max(recent5[0]["close"], 0.01) * 100

        big_net_3 = sum(r["big_net"] for r in recent3)
        small_net_3 = sum(r["small_net"] for r in recent3)
        prev_big_10 = sum(r["big_net"] for r in prev10) if prev10 else 0

        big_strength = sum(r["big_pct"] for r in recent5) / 5 / 100
        retail_strength = sum(r["small_pct"] for r in recent5) / 5 / 100

        closes = [r["close"] for r in rows]
        min_p, max_p = min(closes), max(closes)
        price_position = (last["close"] - min_p) / (max_p - min_p) if max_p > min_p else 0.5

        # 四状态识别
        states = []
        if big_net_5 > 0 and small_net_5 < 0 and price_5_pct > -8:
            states.append("压盘吸筹")
        if prev_big_10 > 0 and big_net_3 < 0 and small_net_3 < 0:
            states.append("洗盘")
        if price_position > self.PRICE_POSITION_DANGER and big_net_5 < 0 and small_net_5 > 0:
            states.append("高位派发")
        if big_net_5 > 0 and price_5_pct < -15 and small_net_5 > 0:
            states.append("统计失真")

        # 概率评分
        acc_score = (0.4 * big_strength + 0.3 * (-retail_strength) + 0.3 * (-price_5_pct / 10)) * 100
        acc_score = max(0, min(100, acc_score + 50))
        dist_score = (0.4 * (-big_strength) + 0.3 * retail_strength + 0.3 * price_position) * 100
        dist_score = max(0, min(100, dist_score + 50))
        total = acc_score + dist_score + 10
        prob_acc = acc_score / total * 100
        prob_dist = dist_score / total * 100

        big_dir = "大↑" if big_net_5 > 0 else "大↓"
        small_dir = "小↑" if small_net_5 > 0 else "小↓"

        # 分类判定（应用四天回测结论）
        grade, reason = self._classify(
            states, price_position, big_strength, big_net_5, small_net_5, prob_dist, price_5_pct
        )

        return FundFlowVerdict(
            code=code, name=name,
            last_close=round(float(last["close"]), 2),
            big_net_5=round(big_net_5, 0),
            small_net_5=round(small_net_5, 0),
            mid_net_5=round(mid_net_5, 0),
            price_5_pct=round(price_5_pct, 2),
            price_position=round(price_position, 4),
            big_strength=round(big_strength, 4),
            retail_strength=round(retail_strength, 4),
            matrix=big_dir + small_dir,
            states_hit=states,
            prob_acc=round(prob_acc, 1),
            prob_dist=round(prob_dist, 1),
            grade=grade, reason=reason,
        )

    def _classify(
        self, states: List[str], price_position: float, big_strength: float,
        big_net_5: float, small_net_5: float, prob_dist: float, price_5_pct: float,
    ) -> tuple:
        """按回测结论分类。返回 (grade, reason)。"""
        # 1. 明确回避：命中高位派发
        if "高位派发" in states:
            return "avoid", "命中高位派发(价格高位+大单流出+散户接盘)"

        # 2. 明确回避：派发概率过高
        if prob_dist >= self.PROB_DIST_AVOID and price_position > self.PRICE_POSITION_DANGER:
            return "avoid", f"高位派发概率{prob_dist:.0f}%+价格位置{price_position*100:.0f}%"

        # 3. 优质买入：价格位置<60% + 大单强度>0 + 命中压盘吸筹 + 5日涨幅<8%
        if ("压盘吸筹" in states
                and price_position < self.PRICE_POSITION_BUY
                and big_strength > self.BIG_STRENGTH_BUY
                and big_net_5 > 0
                and price_5_pct < self.PRICE_5D_BUY_MAX):
            return "buy", "低位压盘吸筹(价格<60%+大单流入+散户流出+5日未追高)"

        # 3b. 压盘吸筹但5日涨幅过大 → 观察而非买入
        if ("压盘吸筹" in states
                and price_position < self.PRICE_POSITION_BUY
                and big_strength > self.BIG_STRENGTH_BUY
                and big_net_5 > 0
                and price_5_pct >= self.PRICE_5D_BUY_MAX):
            return "observe", f"压盘吸筹但5日涨{price_5_pct:.1f}%偏大,获利盘重"

        # 4. 统计失真：资金流参考价值下降
        if "统计失真" in states:
            return "observe", "统计失真(资金流入但价格大跌，数据可疑)"

        # 5. 洗盘状态：可观察
        if "洗盘" in states:
            return "observe", "命中洗盘(前期流入+近期流出+散户恐慌)"

        # 6. 其他：未命中明确信号
        return "observe", "未命中明确信号"

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _load_rows(self, code: str, end_date: Optional[date]) -> List[dict]:
        from sqlalchemy import text
        session = self.db.get_session()
        try:
            sql = (
                "SELECT date, close, pct_chg, big_net, big_pct, mid_net, mid_pct, "
                "small_net, small_pct FROM stock_fund_flow "
                "WHERE code = :code"
            )
            params = {"code": code}
            if end_date is not None:
                sql += " AND date <= :end"
                params["end"] = end_date
            sql += " ORDER BY date ASC"
            result = session.execute(text(sql), params)
            cols = result.keys()
            return [dict(zip(cols, row)) for row in result.fetchall()]
        finally:
            session.close()

    @staticmethod
    def _normalize_date(date_key: Optional[str]) -> Optional[date]:
        if not date_key:
            return None
        s = str(date_key).replace("-", "")
        if len(s) != 8:
            return None
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None


# ----------------------------------------------------------------------
# 格式化输出
# ----------------------------------------------------------------------

def format_verdict_list(verdicts: List[FundFlowVerdict], title: str, show_reason: bool = True) -> str:
    """格式化判定列表（带边框表格）。"""
    if not verdicts:
        return f"{title}: 无"

    from tabulate import tabulate

    headers = ['代码', '名称', '股数', '价位', '5日涨', '大单', '小单', '资金', '吸筹']
    if show_reason:
        headers.append('原因')

    rows = []
    for v in verdicts:
        lots, cost = calc_lots(v.last_close)
        shares_str = f"{lots}手" if lots > 0 else "-"
        row = [
            v.code, v.name,
            shares_str,
            f"{v.price_position*100:.0f}%",
            f"{v.price_5_pct:+.1f}%",
            f"{v.big_net_5/10000:+.2f}亿",
            f"{v.small_net_5/10000:+.2f}亿",
            v.matrix,
            f"{v.prob_acc:.0f}%",
        ]
        if show_reason:
            row.append(v.reason)
        rows.append(row)

    table = tabulate(rows, headers=headers, tablefmt='grid', numalign='right')
    return f"{title} ({len(verdicts)}只)\n{table}"


def format_filter_report(
    buy_list: List[FundFlowVerdict],
    observe_list: List[FundFlowVerdict],
    avoid_list: List[FundFlowVerdict],
) -> str:
    """格式化完整过滤报告。"""
    lines = ["=" * 100, "资金流过滤报告", "=" * 100, ""]

    lines.append(format_verdict_list(buy_list, "[买入] 优质买入（低位压盘吸筹）"))
    lines.append("")
    lines.append(format_verdict_list(observe_list, "[观察] 信号不明确"))
    lines.append("")
    lines.append(format_verdict_list(avoid_list, "[回避] 高位派发风险"))

    lines.append("")
    lines.append("-" * 100)
    lines.append(
        f"汇总: 买入{len(buy_list)} 观察{len(observe_list)} 回避{len(avoid_list)} "
        f"共{len(buy_list)+len(observe_list)+len(avoid_list)}只"
    )
    return "\n".join(lines)
