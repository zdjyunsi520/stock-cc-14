# -*- coding: utf-8 -*-
"""主力资金侦测算法 — 五阶段模型。

阶段:
1. 吸筹 (accumulate)    — 持续15-30天，大单净流入，股价不涨
2. 洗盘 (wash)          — 5-10天，大单流出但远小于吸筹量
3. 二次吸筹 (re_accumulate) — 洗盘后大单重新流入
4. 主升浪启动 (launch)   — 连续流入+放量+突破
5. 派发 (distribute)     — 库存下降超70%，大单大量流出

核心公式:
- accumulate_score = 5日大单净流入总和 / 5日总成交额
- wash_ratio = 近5日大单净流出 / 前15日累计大单净流入
- re_accumulate = 近3日大单净流入 > 近3日大单净流出 且 近5日价格跌幅 < 5%
- 主力库存: 累计大单净流入，下降超70%视为派发

参考: 主力资金侦测算法.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class SmartMoneySignal:
    code: str
    name: str = ""
    date: date = None
    # 各阶段评分 (0-100)
    accumulate_score: float = 0
    wash_score: float = 0
    re_accumulate_score: float = 0
    launch_score: float = 0
    distribute_score: float = 0
    # 综合阶段判断
    phase: str = "unknown"
    confidence: float = 0
    # 虚拟库存
    virtual_position: float = 0
    position_change_pct: float = 0
    # 关键指标快照
    details: Dict = field(default_factory=dict)


@dataclass
class StealthSignal:
    """隐蔽吸筹信号。"""
    code: str
    name: str = ""
    date: date = None
    big_net_sum: float = 0         # N日大单净流入合计（万）
    small_net_sum: float = 0       # N日小单净流入合计（万，吸筹应为负=散户流出）
    inflow_days: int = 0           # 其中大单净流入天数
    total_days: int = 5            # 统计天数
    price_pct: float = 0           # N日股价涨跌幅%
    close: float = 0               # 最新收盘价
    divergence: float = 0          # 背离度 = 大单净流入 / (|涨跌幅|+1)
    strength: float = None         # 最新日主力强度
    big_consecutive: int = 0       # 连续流入天数


class SmartMoneyDetector:
    """主力资金侦测器。"""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def detect(self, code: str, name: str = "") -> SmartMoneySignal:
        """分析单只股票的主力资金阶段。"""
        rows = self._load_data(code)
        if len(rows) < 5:
            return SmartMoneySignal(code=code, name=name)

        signal = SmartMoneySignal(code=code, name=name, date=rows[-1]["date"])

        # 计算虚拟仓位
        self._calc_virtual_position(rows, signal)

        # 五阶段评分
        signal.accumulate_score = self._score_accumulate(rows)
        signal.wash_score = self._score_wash(rows)
        signal.re_accumulate_score = self._score_re_accumulate(rows)
        signal.launch_score = self._score_launch(rows)
        signal.distribute_score = self._score_distribute(rows, signal)

        # 判定当前阶段
        scores = {
            "accumulate": signal.accumulate_score,
            "wash": signal.wash_score,
            "re_accumulate": signal.re_accumulate_score,
            "launch": signal.launch_score,
            "distribute": signal.distribute_score,
        }
        signal.phase = max(scores, key=scores.get)
        signal.confidence = scores[signal.phase]

        # 关键指标快照
        signal.details = self._build_details(rows)

        return signal

    def screen(
        self,
        *,
        codes: Optional[List[str]] = None,
        phase: Optional[str] = None,
        min_confidence: float = 50,
    ) -> List[SmartMoneySignal]:
        """批量筛选主力信号。"""
        if codes is None:
            codes = self._codes_with_fund_data()

        results = []
        for code in codes:
            try:
                sig = self.detect(code)
                if sig.confidence >= min_confidence:
                    if phase is None or sig.phase == phase:
                        results.append(sig)
            except Exception as e:
                logger.debug("detect %s 失败: %s", code, e)

        results.sort(key=lambda s: s.confidence, reverse=True)
        return results

    def screen_stealth(
        self,
        *,
        days: int = 5,
        codes: Optional[List[str]] = None,
        min_inflow_days: int = 3,
    ) -> List[StealthSignal]:
        """筛选隐蔽吸筹：资金持续流入但股价不动甚至下跌。

        核心逻辑: 大单净流入与股价走势的背离 = 主力暗中建仓。
        - 近N日大单净流入 > 0
        - 其中至少 min_inflow_days 天大单净流入（趋势，非单日异常）
        - 股价涨幅很小或下跌
        """
        if codes is None:
            codes = self._codes_with_fund_data()

        results = []
        for code in codes:
            try:
                rows = self._load_data(code)
                if len(rows) < days:
                    continue
                sig = self._calc_stealth(code, rows, days, min_inflow_days)
                if sig is not None:
                    results.append(sig)
            except Exception as e:
                logger.debug("stealth %s 失败: %s", code, e)

        results.sort(key=lambda s: s.divergence, reverse=True)
        return results

    def _calc_stealth(
        self, code: str, rows: List[dict], days: int, min_inflow_days: int
    ) -> Optional[StealthSignal]:
        recent = rows[-days:]

        # 大单净流入总量
        big_net_sum = sum(r.get("big_net") or 0 for r in recent)
        if big_net_sum <= 0:
            return None

        # 大单净流入天数
        inflow_days = sum(1 for r in recent if (r.get("big_net") or 0) > 0)
        if inflow_days < min_inflow_days:
            return None

        # 小单净流入总量：吸筹场景要求散户净流出（small_net_sum < 0）
        # 否则大单+小单一起买，是普涨情绪而非主力独自吸筹
        small_net_sum = sum(r.get("small_net") or 0 for r in recent)
        if small_net_sum >= 0:
            return None

        # 股价涨跌幅
        c_start = recent[0].get("close") or 0
        c_end = recent[-1].get("close") or 0
        price_pct = (c_end - c_start) / max(c_start, 0.01) * 100

        # 背离：股价涨幅越小、大单流入越大 → 信号越强
        # 股价涨 < 2% 才算"暗中"（涨太多就不是偷偷建仓了）
        if price_pct > 3:
            return None

        # divergence = 大单净流入 / (股价涨幅 + 1)，股价跌或平则分母接近0，值极大
        divergence = big_net_sum / (abs(price_pct) + 1)

        # 最新日期数据
        last = rows[-1]
        name = self._get_name(code)

        return StealthSignal(
            code=code,
            name=name,
            date=last.get("date"),
            big_net_sum=round(big_net_sum, 0),
            small_net_sum=round(small_net_sum, 0),
            inflow_days=inflow_days,
            total_days=days,
            price_pct=round(price_pct, 2),
            close=c_end,
            divergence=round(divergence, 0),
            strength=last.get("strength"),
            big_consecutive=last.get("big_consecutive") or 0,
        )

    def _get_name(self, code: str) -> str:
        """从日线表查股票名称。"""
        from sqlalchemy import text
        session = self.db.get_session()
        try:
            r = session.execute(
                text("SELECT code FROM stock_daily WHERE code = :c LIMIT 1"), {"c": code}
            ).first()
            # 日线表没存名称，从 stock_daily_sync_state 取
            r2 = session.execute(
                text("SELECT code_name FROM stock_daily_sync_state WHERE code = :c LIMIT 1"),
                {"c": code},
            ).first()
            return r2[0] if r2 else ""
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 五阶段评分
    # ------------------------------------------------------------------

    def _score_accumulate(self, rows: List[dict]) -> float:
        """吸筹评分 (0-100)。

        文档定义:
        特征: 股价不涨甚至微跌，大单持续流入，主力在接散户筹码
        公式: accumulate_score = 5日大单净流入总和 / 5日总成交额
        吸筹形态: 近5日振幅 < 12%、近5日累计 big_net > 0、价格未创新低

        评分权重 (文档):
        - 大单净流入强度（近3日平均） 40%
        - 连续流入天数 30%
        - 主力占比 20%
        - 价格位置（低位更高分） 10%
        """
        n = len(rows)
        if n < 5:
            return 0

        recent5 = rows[-5:]
        recent15 = rows[-15:] if n >= 15 else rows

        # === 吸筹条件判断 ===
        # 1) 近5日累计大单净流入 > 0
        big_net_5d = sum(r.get("big_net") or 0 for r in recent5)
        if big_net_5d <= 0:
            return 0  # 近5日大单净流出，不可能是吸筹

        # 2) 近5日总成交额
        turnover_5d = sum(
            (r.get("total_inflow") or 0) + (r.get("total_outflow") or 0)
            for r in recent5
        )
        # 没有流入流出明细时，用 net_flow 估算（粗略）
        if turnover_5d == 0:
            turnover_5d = sum(abs(r.get("net_flow") or 0) * 10 for r in recent5)

        # 3) accumulate_score = 5日大单净流入 / 5日总成交额
        accumulate_ratio = big_net_5d / turnover_5d if turnover_5d > 0 else 0

        # 4) 吸筹形态: 近5日振幅 < 12%
        highs = [r.get("close") or 0 for r in recent5 if r.get("close")]
        amplitude_5d = (max(highs) - min(highs)) / max(min(highs), 0.01) * 100 if len(highs) >= 2 else 999

        # 5) 价格未创新低（近5日最低 > 前10日最低）
        if n >= 15:
            low_15d = min(r.get("close") or 999 for r in rows[:-5])
            low_5d = min(r.get("close") or 999 for r in recent5)
            price_not_new_low = low_5d >= low_15d
        else:
            price_not_new_low = True

        # === 评分 ===
        score = 0

        # 强度 (0-40): accumulate_ratio 越大越好
        score += min(40, accumulate_ratio * 400)

        # 连续流入天数 (0-30)
        consec = recent5[-1].get("big_consecutive") or 0
        score += min(30, consec * 5)

        # 主力占比 (0-20)
        ratio_avg = sum(r.get("big_ratio") or 0 for r in recent5) / 5
        score += min(20, ratio_avg * 50)

        # 价格位置 (0-10): 股价不涨甚至微跌，低位得分更高
        price_5d = ((recent5[-1]["close"] or 0) - (recent5[0]["close"] or 0)) / max(recent5[0]["close"] or 1, 0.01) * 100
        if price_5d < 2:
            score += 10
        elif price_5d < 5:
            score += 5

        # 吸筹形态加分
        if amplitude_5d < 12:
            score += 5
        if price_not_new_low:
            score += 5

        return round(min(100, max(0, score)), 1)

    def _score_wash(self, rows: List[dict]) -> float:
        """洗盘评分 (0-100)。

        文档定义:
        特征: 股价跌、大单流出、但流出量远小于吸筹量、小单恐慌卖出
        wash_ratio = 近5日大单净流出 / 前15日累计大单净流入
        关键: 70%以上大概率真跑路（派发），洗盘流出占比小

        区别于派发:
        洗盘: 吸筹5000万 流出500万 (占比小)
        派发: 吸筹5000万 流出4500万 (占比大)
        """
        n = len(rows)
        if n < 15:
            return 0

        # 前15日累计大单净流入（吸筹量）
        prev15 = rows[-20:-5] if n >= 20 else rows[:-5]
        prev_big_sum = sum(r.get("big_net") or 0 for r in prev15)

        if prev_big_sum <= 0:
            return 0  # 前期没有吸筹，不存在洗盘

        # 近5日大单净流出
        recent5 = rows[-5:]
        recent_big_sum = sum(r.get("big_net") or 0 for r in recent5)

        # wash_ratio: 流出占吸筹的比例
        if recent_big_sum >= 0:
            return 0  # 近5日仍在流入，不是洗盘

        wash_ratio = abs(recent_big_sum) / prev_big_sum

        # === 洗盘 vs 派发 判断 ===
        # wash_ratio < 30%: 很可能洗盘
        # wash_ratio 30%-70%: 不确定
        # wash_ratio > 70%: 大概率派发

        score = 0

        # 洗盘比率 (0-40): 流出占比越像"温和洗盘"得分越高
        if wash_ratio <= 0.3:
            score += 40  # 典型洗盘：流出远小于吸筹
        elif wash_ratio <= 0.5:
            score += 30
        elif wash_ratio <= 0.7:
            score += 15  # 接近派发边界
        else:
            return 0  # 超过70%，直接判派发而非洗盘

        # 近5日股价下跌 (0-20): 温和下跌最像洗盘
        price_5d = ((recent5[-1]["close"] or 0) - (recent5[0]["close"] or 0)) / max(recent5[0]["close"] or 1, 0.01) * 100
        if -8 < price_5d < -1:
            score += 20
        elif -15 < price_5d <= -8:
            score += 10

        # 小单恐慌卖出/流入 (0-20): 小单净流入正 = 散户在接盘
        small_net_5d = sum(r.get("small_net") or 0 for r in recent5)
        if small_net_5d > 0:
            score += 20  # 散户接盘，典型洗盘
        elif small_net_5d > -500:
            score += 10

        # 大单流出但绝对量不大 (0-20)
        if abs(recent_big_sum) < prev_big_sum * 0.3:
            score += 20
        elif abs(recent_big_sum) < prev_big_sum * 0.5:
            score += 10

        return round(min(100, max(0, score)), 1)

    def _score_re_accumulate(self, rows: List[dict]) -> float:
        """二次吸筹评分 (0-100)。

        文档定义:
        洗盘结束后: 股价创新低失败，大单重新流入
        re_accumulate = 近3日大单净流入 > 近3日大单净流出
        且 近5日价格跌幅 < 5%

        前提: 必须先有吸筹→洗盘的完整过程
        """
        n = len(rows)
        if n < 15:
            return 0

        recent3 = rows[-3:]
        recent5 = rows[-5:]

        # === 前提: 必须有洗盘痕迹 ===
        # 前5-15日有大单净流出，且流出占前期吸筹的10%-70%
        wash_zone = rows[-15:-5] if n >= 15 else rows[:-5]
        accumulate_zone = rows[-25:-15] if n >= 25 else (rows[:max(1, len(rows) - 15)] if n > 15 else [])

        if wash_zone:
            wash_big_sum = sum(r.get("big_net") or 0 for r in wash_zone)
            if wash_big_sum >= 0:
                return 0  # 没有洗盘，不是二次吸筹

            if accumulate_zone:
                acc_big_sum = sum(r.get("big_net") or 0 for r in accumulate_zone)
                if acc_big_sum <= 0:
                    return 0  # 没有前期吸筹
                wash_ratio = abs(wash_big_sum) / acc_big_sum
                if wash_ratio < 0.1:
                    return 0  # 流出太轻微，不算洗盘
                if wash_ratio > 0.7:
                    return 0  # 流出太多，已经是派发

        # === 二次吸筹条件 ===
        # 1) 近3日大单净流入 > 0
        big_net_3d = sum(r.get("big_net") or 0 for r in recent3)
        if big_net_3d <= 0:
            return 0

        # 2) 近5日价格跌幅 < 5%
        price_5d = ((recent5[-1]["close"] or 0) - (recent5[0]["close"] or 0)) / max(recent5[0]["close"] or 1, 0.01) * 100
        if price_5d < -5:
            return 0

        # === 评分 ===
        score = 0

        # 近3日大单净流入强度 (0-40)
        score += min(40, big_net_3d / 500 * 20)

        # 价格企稳 (0-25)
        if -3 < price_5d < 3:
            score += 25
        elif -5 < price_5d < 5:
            score += 15

        # 洗盘→二次吸筹的转折确认 (0-20)
        if wash_zone:
            score += 20

        # 连续流入天数 (0-15)
        consec = recent3[-1].get("big_consecutive") or 0
        score += min(15, consec * 5)

        return round(min(100, max(0, score)), 1)

    def _score_launch(self, rows: List[dict]) -> float:
        """主升浪启动评分 (0-100)。

        文档定义:
        近3日大单净流入 > 0 且 连续2天放量 且 突破20日高点 — 这是买点
        """
        n = len(rows)
        if n < 20:
            return 0

        recent3 = rows[-3:]

        # === 条件判断 ===
        # 1) 近3日大单净流入 > 0
        big_net_3d = sum(r.get("big_net") or 0 for r in recent3)
        if big_net_3d <= 0:
            return 0

        # 2) 连续2天放量
        vols = [abs(r.get("net_flow") or 0) for r in rows[-10:]]
        vol_recent2 = sum(vols[-2:]) / 2 if len(vols) >= 2 else 0
        vol_prev5 = sum(vols[-7:-2]) / 5 if len(vols) >= 7 else 1
        volume_surge = vol_recent2 > vol_prev5 * 1.5 if vol_prev5 > 0 else False

        # 3) 突破20日高点
        high_20 = max(r.get("close") or 0 for r in rows[-20:])
        current_close = recent3[-1].get("close") or 0
        breakout = current_close >= high_20

        # === 评分 ===
        score = 0

        # 大单净流入强度 (0-30)
        score += min(30, big_net_3d / 1000 * 15)

        # 突破20日高点 (0-35)
        if breakout:
            score += 35
        elif current_close >= high_20 * 0.98:
            score += 20
        elif current_close >= high_20 * 0.95:
            score += 10

        # 成交量放大 (0-20)
        if volume_surge:
            score += 20
        elif vol_recent2 > vol_prev5:
            score += 10

        # 连续流入 (0-15)
        consec = recent3[-1].get("big_consecutive") or 0
        if consec >= 2:
            score += 15
        elif consec >= 1:
            score += 8

        return round(min(100, max(0, score)), 1)

    def _score_distribute(self, rows: List[dict], signal: SmartMoneySignal) -> float:
        """派发评分 (0-100)。

        文档定义:
        区别于洗盘:
        洗盘: 吸筹5000万 流出500万 流出占比小
        派发: 吸筹5000万 流出4500万 流出占比很大
        本质: 主力库存没了

        主力库存下降超过70%，主力基本走完。
        wash_ratio > 70% → 派发
        """
        n = len(rows)
        if n < 10:
            return 0

        recent3 = rows[-3:]
        recent5 = rows[-5:]

        # === 派发核心: 虚拟仓位下降 ===
        # 文档: "如果库存下降超过70%，主力基本走完"
        score = 0

        # 虚拟仓位下降幅度 (0-35)
        if signal.position_change_pct < -70:
            score += 35
        elif signal.position_change_pct < -50:
            score += 25
        elif signal.position_change_pct < -30:
            score += 15

        # 近5日大单净流出 (0-25)
        big_net_5d = sum(r.get("big_net") or 0 for r in recent5)
        if big_net_5d < 0:
            # wash_ratio 判断
            prev = rows[-20:-5] if n >= 20 else rows[:-5]
            prev_big_sum = sum(r.get("big_net") or 0 for r in prev)
            if prev_big_sum > 0:
                wash_ratio = abs(big_net_5d) / prev_big_sum
                if wash_ratio > 0.7:
                    score += 25  # 明确派发
                elif wash_ratio > 0.5:
                    score += 15
                elif wash_ratio > 0.3:
                    score += 5
            else:
                score += 10  # 前期无吸筹但大单流出

        # 小单流入（散户接盘）(0-20)
        small_net_5d = sum(r.get("small_net") or 0 for r in recent5)
        if small_net_5d > 0:
            score += 20

        # 股价高位: 近15日涨幅 > 15% (0-20)
        if n >= 15:
            price_15d = ((rows[-1]["close"] or 0) - (rows[-15]["close"] or 0)) / max(rows[-15]["close"] or 1, 0.01) * 100
            if price_15d > 15:
                score += 20
            elif price_15d > 8:
                score += 10

        return round(min(100, max(0, score)), 1)

    # ------------------------------------------------------------------
    # 虚拟仓位
    # ------------------------------------------------------------------

    def _calc_virtual_position(self, rows: List[dict], signal: SmartMoneySignal) -> None:
        """计算主力虚拟仓位。

        从最早数据开始，累加大单净流入作为虚拟仓位。
        如果仓位从峰值下降超过70%，主力基本走完。
        """
        position = 0.0
        peak_position = 0.0

        for r in rows:
            big_net = r.get("big_net") or 0
            position += big_net
            peak_position = max(peak_position, position)

        signal.virtual_position = round(position, 2)

        if peak_position > 0:
            signal.position_change_pct = round(position / peak_position * 100, 1)
        else:
            signal.position_change_pct = 0

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _load_data(self, code: str) -> List[dict]:
        """加载全部可用的资金流向数据。"""
        from sqlalchemy import text
        session = self.db.get_session()
        try:
            result = session.execute(
                text(
                    "SELECT date, close, pct_chg, net_flow, big_net, big_pct, "
                    "mid_net, mid_pct, small_net, small_pct, "
                    "total_inflow, total_outflow, big_inflow, big_outflow, "
                    "mid_inflow, mid_outflow, small_inflow, small_outflow, "
                    "strength, big_ratio, big_consecutive "
                    "FROM stock_fund_flow "
                    "WHERE code = :code "
                    "ORDER BY date ASC"
                ),
                {"code": code},
            )
            cols = result.keys()
            return [dict(zip(cols, row)) for row in result.fetchall()]
        finally:
            session.close()

    def _codes_with_fund_data(self) -> List[str]:
        """获取有资金流向数据的股票列表。"""
        from sqlalchemy import text
        session = self.db.get_session()
        try:
            result = session.execute(
                text("SELECT DISTINCT code FROM stock_fund_flow ORDER BY code")
            )
            return [row[0] for row in result.fetchall()]
        finally:
            session.close()

    def _build_details(self, rows: List[dict]) -> dict:
        """构建关键指标快照。"""
        r = rows[-1]
        n = len(rows)
        recent3 = rows[-3:]
        recent5 = rows[-5:]

        # 前15日累计大单净流入
        prev15 = rows[-20:-5] if n >= 20 else rows[:-5]
        prev_big_sum = sum(x.get("big_net") or 0 for x in prev15) if prev15 else 0

        # wash_ratio
        recent_big_5d = sum(x.get("big_net") or 0 for x in recent5)
        wash_ratio = abs(recent_big_5d) / prev_big_sum if prev_big_sum > 0 and recent_big_5d < 0 else 0

        return {
            "close": r.get("close"),
            "pct_chg": r.get("pct_chg"),
            "big_net_today": r.get("big_net"),
            "big_consecutive": r.get("big_consecutive"),
            "strength": r.get("strength"),
            "big_ratio": r.get("big_ratio"),
            "big_net_3d": sum(x.get("big_net") or 0 for x in recent3),
            "big_net_5d": recent_big_5d,
            "small_net_3d": sum(x.get("small_net") or 0 for x in recent3),
            "small_net_5d": sum(x.get("small_net") or 0 for x in recent5),
            "price_5d_pct": round(
                ((recent5[-1].get("close") or 0) - (recent5[0].get("close") or 0))
                / max(recent5[0].get("close") or 1, 0.01) * 100, 2
            ),
            "high_20d": max(x.get("close") or 0 for x in rows[-20:]) if n >= 20 else None,
            "prev_15d_big_sum": round(prev_big_sum, 0),
            "wash_ratio": round(wash_ratio, 4),
            "data_days": n,
        }


def format_signal(sig: SmartMoneySignal) -> str:
    """格式化输出信号。"""
    phase_cn = {
        "accumulate": "吸筹",
        "wash": "洗盘",
        "re_accumulate": "二次吸筹",
        "launch": "主升浪",
        "distribute": "派发",
        "unknown": "未知",
    }
    d = sig.details
    lines = [
        f"=== {sig.code} {sig.name} ===",
        f"数据天数: {d.get('data_days', 0)}",
        f"阶段: {phase_cn.get(sig.phase, sig.phase)} ({sig.confidence}分)",
        f"虚拟仓位: {sig.virtual_position:.0f}万  仓位比: {sig.position_change_pct:.0f}%",
        f"收盘: {d.get('close')}  涨跌: {d.get('pct_chg')}%",
        f"今日大单净额: {d.get('big_net_today', 0):.0f}万  连续流入: {d.get('big_consecutive', 0)}天",
        f"3日大单: {d.get('big_net_3d', 0):.0f}万  5日大单: {d.get('big_net_5d', 0):.0f}万",
        f"5日涨跌: {d.get('price_5d_pct', 0):.1f}%  20日高点: {d.get('high_20d')}",
        f"前15日吸筹: {d.get('prev_15d_big_sum', 0):.0f}万  wash_ratio: {d.get('wash_ratio', 0):.2%}",
        f"--- 各阶段评分 ---",
        f"  吸筹: {sig.accumulate_score}  洗盘: {sig.wash_score}  二次吸筹: {sig.re_accumulate_score}",
        f"  主升浪: {sig.launch_score}  派发: {sig.distribute_score}",
    ]
    return "\n".join(lines)


def format_stealth_list(signals: List[StealthSignal]) -> str:
    """格式化隐蔽吸筹列表。"""
    if not signals:
        return "未发现隐蔽吸筹信号"

    lines = [
        f"隐蔽吸筹筛选: {len(signals)}只 (大单持续流入 + 小单净流出 + 股价不动/下跌)",
        "-" * 110,
        f"{'代码':<8} {'名称':<8} {'收盘':>8} {'5日涨跌':>8} {'大单净流入':>10} {'小单净流入':>10} {'流入天数':>8} {'背离度':>8}",
        "-" * 110,
    ]
    for s in signals[:30]:
        price_tag = f"{s.price_pct:+.1f}%"
        big_tag = f"{s.big_net_sum:.0f}万"
        small_tag = f"{s.small_net_sum:.0f}万"
        lines.append(
            f"{s.code:<8} {s.name:<8} {s.close:>8.2f} {price_tag:>8} {big_tag:>10} "
            f"{small_tag:>10} {s.inflow_days}/{s.total_days:>4} {s.divergence:>8.0f}"
        )
    return "\n".join(lines)
