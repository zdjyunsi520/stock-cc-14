# -*- coding: utf-8 -*-
"""分时吸筹识别（基于价格档成交量分布）。

参考: 分时侦测算法.md + 用户对话："改成相同价格的成交量"

核心思想: 吸筹 = 大量筹码在同一价格区间反复交换。
- 不再按时间窗口（30min 斜率、3min 急跌+10min 反弹）判定
- 按当日均价的 0.5% 分价格档，每档累计成交量
- 找出 POC（成交最密集档）和密集区（Value Area）
- 评分 = POC 集中度 + 密集区占比 + 紧密度 + 价格回归 POC 次数

数据约束:
- 1min K 表无换手率字段，用 volume 直接累加
- 强制按日内切，避免跨日跳空
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple, Union

from src.storage import DatabaseManager, Stock1minKline

logger = logging.getLogger(__name__)


@dataclass
class AccumulationResult:
    """单个交易日的价格档吸筹分析结果。"""
    code: str
    date: date
    start: datetime
    end: datetime
    minutes: int
    # 基准
    price_ref: float = 0          # 当日均价（成交额/成交量）
    bin_size: float = 0           # 价格档粒度（元）
    n_bins: int = 0               # 不同价格档数
    total_vol: int = 0            # 当日总成交量
    # POC（成交最密集档）
    poc_price: float = 0          # POC 档中点价
    poc_vol: int = 0              # POC 档成交量
    poc_pct: float = 0            # POC 档占总成交量 %
    poc_count: int = 0            # POC 档被触及的 1min K 数
    # 密集区 Value Area
    va_lo: float = 0              # 密集区下沿价
    va_hi: float = 0              # 密集区上沿价
    va_pct: float = 0             # 密集区占总成交量 %
    va_bins: int = 0              # 密集区档数
    # 形态
    tight_ratio: float = 0        # 紧密度：密集区档数 / 总档数（越小越集中）
    range_pct: float = 0          # 窄幅震荡占比：落在 VA 内的 1min 根数 %
    close_pos: str = ""           # 收盘价位置分类：PRESS_LOW / IN_VA / ABOVE_POC / BREAK_UP
    close_vs_avg: float = 0       # 收盘相对均价 %
    close_vs_poc: float = 0       # 收盘距 POC 的绝对值 %
    # K线形态（十字星核心）
    open_price: float = 0         # 开盘价（1min 首根）
    body_pct: float = 0           # 实体占价格% = |收-开|/开（核心评分信号，越小越像十字星）
    wick_ratio: float = 0         # 影线占振幅比 = 1 - 实体/振幅（越大越像星形）
    min_wick: float = 0           # 双侧较小影线% = min(上影,下影)（真十字星上下都有影线）
    # 趋势确认（vs 前5日，需 1min 历史卷）
    shrink_ratio: float = 0       # 量比：当日成交量 / 前5日均量（越小越缩量=见底）
    decay_ratio: float = 0        # 动能衰竭：当日振幅 / 前5日均振幅（越小越衰竭=见底）
    # 资金面（当日，仅展示，不参与评分——大样本回测显示对次日无预测力）
    big_net: float = 0             # 大单净额（万元）
    big_pct: float = 0             # 大单净占比 %
    big_consecutive: int = 0       # 连续大单净流入天数
    # 评分（十字星超短线识别 100）：
    #   实体占价格% 40 + 影线占比 30 + 双侧影线门槛 30
    # 数据缺失日直接 0 分。
    score: int = 0
    score_pos: int = 0          # 实体占价格% (max 40，越小越好)
    score_decay: int = 0        # 影线占比 (max 30，越大越好)
    score_consec: int = 0       # 双侧影线门槛 (max 30，min(上,下)影越大越好)
    score_shrink: int = 0       # 保留字段，恒为 0
    score_big: int = 0          # 保留字段，恒为 0
    # 以下保留字段（旧形态分项），恒为 0，仅兼容展示
    score_poc: int = 0
    score_va: int = 0
    score_tight: int = 0
    score_range: int = 0
    score_big_pct: int = 0
    score_big_consec: int = 0
    state: str = "NORMAL"    # NORMAL / SUSPECT / ACCUMULATE / INSTITUTIONAL


class MinuteAccumulationDetector:
    """价格档成交量分布侦测器。"""

    BIN_SIZE_PCT = 0.002         # 价格档粒度：均价的 0.2%（0.5%档数太少致POC无区分度）
    VA_TARGET_PCT = 0.70         # 密集区目标：包含 70% 成交量
    MIN_DAY_MINUTES = 120        # 最小交易日分钟数（半个交易日）
    LOOKBACK_DAYS = 5            # 趋势确认回看天数（缩量/动能衰竭）

    # 评分阈值（十字星超短线识别）
    # 核心：实体极小（开收接近）+ 影线占比大（星形）+ 双侧影线都有
    # 实体占价格%（|收-开|/开，越小越像十字星）
    BODY_PCT_FULL = 0.10        # 实体 <0.10% 满分（真十字星）
    BODY_PCT_ZERO = 0.60        # >0.60% 零分（兼容0.49%的05-22类小实体）
    # 影线占振幅比（1 - 实体/振幅，越大越像星形）
    WICK_RATIO_FULL = 90        # 影线占振幅 ≥90% 满分
    WICK_RATIO_ZERO = 60        # <60% 零分
    # 双侧影线门槛（min(上影,下影)占价格%，越小越不像十字星）
    # 真十字星上下都有影线；T字星/锤子线只有一侧→排除
    MIN_WICK_FULL = 0.50        # 双侧较小影线 ≥0.50% 满分
    MIN_WICK_ZERO = 0.10        # <0.10% 零分（几乎单侧）
    # 当日涨跌绝对值%（越小越好，十字星当天波动小）
    CHG_ABS_FULL = 0.5
    CHG_ABS_ZERO = 1.2
    # 以下保留阈值（旧字段，不评分）
    ZOMBIE_AMP_MAX = 1.5
    SHRINK_FULL = 0.70
    SHRINK_ZERO = 1.20
    BIG_MILD_FULL = 3.0
    BIG_MILD_ZERO = 10.0
    CLOSE_VS_POC_FULL = 0.15
    CLOSE_VS_POC_ZERO = 0.80
    DECAY_FULL = 0.60
    DECAY_ZERO = 1.20
    POC_PCT_FULL = 30.0
    POC_PCT_ZERO = 8.0
    VA_PCT_FULL = 80.0
    VA_PCT_ZERO = 68.0
    TIGHT_FULL = 0.40
    TIGHT_ZERO = 0.70
    RANGE_FULL = 92.0
    RANGE_ZERO = 70.0
    CLOSE_LOW_FULL = -2.0
    CLOSE_LOW_ZERO = 1.0
    BIG_PCT_FULL = 8.0
    BIG_PCT_ZERO = 0.0
    BIG_CONSEC_FULL = 3
    BIG_CONSEC_ZERO = 0
    BIG_MILD_LO = -10.0
    BIG_MILD_HI = 0.0

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def detect(
        self,
        code: str,
        start: Union[str, date, datetime],
        end: Union[str, date, datetime],
    ) -> List[AccumulationResult]:
        """检测区间内每个交易日的价格档吸筹得分。"""
        start_dt, end_dt = self._normalize_range(start, end)

        bars = self._load_bars(code, start_dt, end_dt)
        if not bars:
            logger.warning(
                "detect %s: 区间 %s~%s 无分时数据",
                code, start_dt.date(), end_dt.date(),
            )
            return []

        # 加载对应区间资金流（按日）
        fund_by_date = self._load_fund_flow(code, start_dt, end_dt)

        by_day: dict = defaultdict(list)
        for b in bars:
            by_day[b.ts.date()].append(b)

        results: List[AccumulationResult] = []
        sorted_days = sorted(by_day.keys())
        for i, d in enumerate(sorted_days):
            day_bars = by_day[d]
            if len(day_bars) < self.MIN_DAY_MINUTES:
                continue
            # 前 N 个交易日的 bars（用于缩量分、动能衰竭分）
            prev_days = [by_day[pd] for pd in sorted_days[max(0, i - self.LOOKBACK_DAYS):i]]
            r = self._analyze(code, d, day_bars, fund_by_date.get(d), prev_days)
            if r:
                results.append(r)

        logger.info(
            "detect %s: %s~%s 共 %d 个交易日, 评分 %d 天",
            code, start_dt.date(), end_dt.date(), len(by_day), len(results),
        )
        return results

    def _load_bars(self, code: str, start_dt: datetime, end_dt: datetime) -> list:
        session = self.db.get_session()
        try:
            return session.query(Stock1minKline).filter(
                Stock1minKline.code == code,
                Stock1minKline.ts >= start_dt,
                Stock1minKline.ts < end_dt + timedelta(days=1),
            ).order_by(Stock1minKline.ts).all()
        finally:
            session.close()

    def _load_fund_flow(self, code: str, start_dt: datetime, end_dt: datetime) -> dict:
        """加载区间内每日资金流，返回 {date: StockFundFlow}。"""
        from src.storage import StockFundFlow
        session = self.db.get_session()
        try:
            flows = session.query(StockFundFlow).filter(
                StockFundFlow.code == code,
                StockFundFlow.date >= start_dt.date(),
                StockFundFlow.date <= end_dt.date(),
            ).all()
            return {f.date: f for f in flows}
        finally:
            session.close()

    def _analyze(
        self,
        code: str,
        d: date,
        bars: list,
        fund_flow: Optional[object] = None,
        prev_days: Optional[list] = None,
    ) -> Optional[AccumulationResult]:
        # 当日均价（成交额/成交量）
        total_amount = sum(b.amount for b in bars if b.amount > 0)
        total_vol = sum(b.volume for b in bars if b.volume > 0)
        if total_vol <= 0:
            return None
        price_ref = total_amount / total_vol if total_amount > 0 else bars[0].price
        bin_size = max(0.01, price_ref * self.BIN_SIZE_PCT)

        # 价格档分布
        dist: dict = defaultdict(lambda: {"volume": 0, "amount": 0.0, "count": 0})
        for b in bars:
            if b.price <= 0 or b.volume <= 0:
                continue
            bin_idx = int(b.price / bin_size)
            dist[bin_idx]["volume"] += b.volume
            dist[bin_idx]["amount"] += b.amount
            dist[bin_idx]["count"] += 1
        if not dist:
            return None

        # POC
        poc_idx = max(dist.keys(), key=lambda k: dist[k]["volume"])
        poc_vol = dist[poc_idx]["volume"]
        poc_pct = poc_vol / total_vol * 100

        # 密集区 Value Area：从 POC 向两侧扩展至 70% 成交量
        va_lo_idx, va_hi_idx, va_vol, va_bins = self._value_area(dist, poc_idx, total_vol)
        va_pct = va_vol / total_vol * 100

        poc_price = (poc_idx + 0.5) * bin_size
        n_bins = len(dist)

        # 紧密度：密集区档数 / 总档数（越少越集中，反映"窄盘"）
        tight_ratio = va_bins / n_bins if n_bins > 0 else 1.0

        # 窄幅震荡占比：落在 VA 内的 1min 根数占比（吸筹=长时间在密集区震荡）
        range_pct = self._calc_range_pct(bars, va_lo_idx, va_hi_idx, bin_size)

        # 收盘价位置（仅作展示分类）—— 收盘价优先用资金流close，缺失则用1min末根
        close_price = float(fund_flow.close) if fund_flow and fund_flow.close else bars[-1].price
        close_pos = self._classify_close(close_price, poc_price, va_lo_idx, va_hi_idx, bin_size)
        close_vs_avg = (close_price - price_ref) / price_ref * 100 if price_ref > 0 else 0.0
        close_vs_poc = abs((close_price - poc_price) / poc_price * 100) if poc_price > 0 else 99.0
        pct_chg = float(fund_flow.pct_chg) if fund_flow and fund_flow.pct_chg is not None else None

        # 趋势确认：缩量分 + 动能衰竭分（vs 前 N 日 1min 历史卷）
        prices = [b.price for b in bars if b.price > 0]
        cur_amp = (max(prices) - min(prices)) / price_ref * 100 if price_ref > 0 else 0.0
        shrink_ratio, decay_ratio, prev_avg_amp = self._calc_trend(total_vol, cur_amp, prev_days)

        # K线实体/影线（十字星核心）：开盘=1min首根，收盘=1min末根（纯1min，不依赖资金表）
        open_price = prices[0] if prices else 0.0
        close_price_k = prices[-1] if prices else 0.0  # 纯1min收盘
        body = abs(close_price_k - open_price)
        body_pct = body / open_price * 100 if open_price > 0 else 99.0
        day_high = max(prices) if prices else 0.0
        day_low = min(prices) if prices else 0.0
        day_range = day_high - day_low
        wick_ratio = (1 - body / day_range) * 100 if day_range > 0 else 0.0
        # 双侧影线（真十字星上下都有影线）
        upper_wick = (day_high - max(open_price, close_price_k)) / open_price * 100 if open_price > 0 else 0.0
        lower_wick = (min(open_price, close_price_k) - day_low) / open_price * 100 if open_price > 0 else 0.0
        min_wick = min(upper_wick, lower_wick)

        # 资金面（仅展示，不参与评分）
        big_net = float(fund_flow.big_net or 0) if fund_flow else 0.0
        big_pct_raw = fund_flow.big_pct if fund_flow else None
        big_pct = float(big_pct_raw) if big_pct_raw is not None else None
        big_consec = int(fund_flow.big_consecutive or 0) if fund_flow else 0

        # 评分（十字星超短线识别）—— 纯1min数据，不依赖资金表
        # 数据完整性：1min数据有效即可（open/high/low 都算出来了）
        has_data = open_price > 0 and day_range >= 0
        score, s_body, s_wick, s_minwick, s_shrink, s_big, s_poc, s_va, s_tight, s_range = self._calc_score(
            body_pct, wick_ratio, min_wick, has_data,
        )
        state = self._state_from_score(score)

        return AccumulationResult(
            code=code,
            date=d,
            start=bars[0].ts,
            end=bars[-1].ts,
            minutes=len(bars),
            price_ref=price_ref,
            bin_size=bin_size,
            n_bins=n_bins,
            total_vol=total_vol,
            poc_price=poc_price,
            poc_vol=poc_vol,
            poc_pct=poc_pct,
            poc_count=dist[poc_idx]["count"],
            va_lo=(va_lo_idx + 0.5) * bin_size,
            va_hi=(va_hi_idx + 0.5) * bin_size,
            va_pct=va_pct,
            va_bins=va_bins,
            tight_ratio=tight_ratio,
            range_pct=range_pct,
            close_pos=close_pos,
            close_vs_avg=close_vs_avg,
            close_vs_poc=close_vs_poc,
            open_price=open_price,
            body_pct=body_pct,
            wick_ratio=wick_ratio,
            min_wick=min_wick,
            shrink_ratio=shrink_ratio,
            decay_ratio=decay_ratio,
            big_net=big_net,
            big_pct=big_pct if big_pct is not None else 0.0,
            big_consecutive=big_consec,
            score=score,
            score_pos=s_body,
            score_decay=s_wick,
            score_consec=s_minwick,
            score_shrink=s_shrink,
            score_big=s_big,
            score_poc=s_poc,
            score_va=s_va,
            score_tight=s_tight,
            score_range=s_range,
            score_big_pct=0,
            score_big_consec=0,
            state=state,
        )

    def _value_area(
        self,
        dist: dict,
        poc_idx: int,
        total_vol: int,
    ) -> Tuple[int, int, int, int]:
        """市场轮廓 VA 算法：从 POC 向两侧扩展，每次加入成交量更大的邻居。

        返回 (va_lo_idx, va_hi_idx, va_vol, va_bins)。
        """
        sorted_bins = sorted(dist.keys())
        target = total_vol * self.VA_TARGET_PCT

        in_va = {poc_idx}
        va_vol = dist[poc_idx]["volume"]
        lo_pos = sorted_bins.index(poc_idx)
        hi_pos = lo_pos

        while va_vol < target and len(in_va) < len(dist):
            left_bin = sorted_bins[lo_pos - 1] if lo_pos > 0 else None
            right_bin = sorted_bins[hi_pos + 1] if hi_pos < len(sorted_bins) - 1 else None
            left_vol = dist[left_bin]["volume"] if left_bin and left_bin not in in_va else -1
            right_vol = dist[right_bin]["volume"] if right_bin and right_bin not in in_va else -1

            if left_vol < 0 and right_vol < 0:
                break
            if right_vol > left_vol:
                in_va.add(right_bin)
                va_vol += right_vol
                hi_pos += 1
            else:
                in_va.add(left_bin)
                va_vol += left_vol
                lo_pos -= 1

        return min(in_va), max(in_va), va_vol, len(in_va)

    @staticmethod
    def _calc_range_pct(bars: list, va_lo_idx: int, va_hi_idx: int, bin_size: float) -> float:
        """窄幅震荡占比：落在密集区 [va_lo_idx, va_hi_idx] 内的 1min 根数占比 (%)。

        吸筹特征是价格长时间在密集区震荡，占比越高越好。取代旧的"穿越POC次数"
        ——后者只反映价格噪声，1min 数据下必然几十次，无区分度。
        """
        if not bars or bin_size <= 0:
            return 0.0
        in_va = 0
        total = 0
        for b in bars:
            if b.price <= 0:
                continue
            total += 1
            idx = int(b.price / bin_size)
            if va_lo_idx <= idx <= va_hi_idx:
                in_va += 1
        return in_va / total * 100 if total > 0 else 0.0

    @staticmethod
    def _calc_trend(cur_vol: int, cur_amp: float, prev_days: Optional[list]) -> tuple:
        """趋势确认：缩量比 + 动能衰竭比 + 前N日均振幅（当日 vs 前 N 日 1min 历史卷）。

        返回 (shrink_ratio, decay_ratio, prev_avg_amp)：
          shrink_ratio = 当日成交量 / 前 N 日均成交量（越小越缩量）
          decay_ratio  = 当日振幅   / 前 N 日均振幅   （越小越衰竭）
          prev_avg_amp = 前 N 日平均振幅%（用于僵尸股过滤）
        前 N 日数据不足时缩量/衰竭返回 1.0，prev_avg_amp 返回 0。
        """
        if not prev_days:
            return 1.0, 1.0, 0.0
        prev_vols = []
        prev_amps = []
        for dbars in prev_days:
            v = sum(b.volume for b in dbars if b.volume > 0)
            a = sum(b.amount for b in dbars if b.amount > 0)
            prices = [b.price for b in dbars if b.price > 0]
            if v <= 0 or a <= 0 or not prices:
                continue
            avg = a / v
            prev_vols.append(v)
            prev_amps.append((max(prices) - min(prices)) / avg * 100)
        if not prev_vols:
            return 1.0, 1.0, 0.0
        avg_vol = sum(prev_vols) / len(prev_vols)
        avg_amp = sum(prev_amps) / len(prev_amps)
        shrink = cur_vol / avg_vol if avg_vol > 0 else 1.0
        decay = cur_amp / avg_amp if avg_amp > 0 else 1.0
        return shrink, decay, avg_amp

    @staticmethod
    def _classify_close(close_price: float, poc_price: float, va_lo_idx: int, va_hi_idx: int, bin_size: float) -> str:
        """收盘价相对密集区的位置分类（客观分类，评分方向见 _close_pos_score）。

        全样本回测（3万+样本）发现：收盘越低于日内均价/POC，次日表现越好
        ——符合"主力压价吸筹/洗盘到低位"的语义；收盘强势反而是追高盘次日回落。

        分类（客观）：
        PRESS_LOW  : 收盘低于 VA 下沿（压价吸筹，回测最优）
        IN_VA      : 收盘落在 VA 内但低于 POC（偏弱吸筹）
        ABOVE_POC  : 收盘在 POC 上方但未突破 VA（偏强，中性偏弱信号）
        BREAK_UP   : 收盘突破 VA 上沿（追高，回测最差）
        """
        if close_price <= 0 or bin_size <= 0:
            return "ABOVE_POC"
        idx = int(close_price / bin_size)
        if idx > va_hi_idx:
            return "BREAK_UP"
        if idx < va_lo_idx:
            return "PRESS_LOW"
        if close_price >= poc_price:
            return "ABOVE_POC"
        return "IN_VA"

    def _calc_score(
        self,
        body_pct: float,
        wick_ratio: float,
        min_wick: float,
        has_data: bool,
    ) -> tuple:
        """评分 0-100，十字星超短线识别。

        返回 (total, body, wick, minwick, 0,0,0,0,0,0)。

        实体占价格% 40（越小越好——开收接近）
        影线占比 30（越大越好——实体外的振幅占比）
        双侧影线门槛 30（min(上影,下影)越大越好——真十字星上下都有影线）

        数据缺失直接返回全 0。
        """
        if not has_data:
            return 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        s_body = self._scale_inv(body_pct, self.BODY_PCT_FULL, self.BODY_PCT_ZERO, 40)
        s_wick = self._scale(wick_ratio, self.WICK_RATIO_ZERO, self.WICK_RATIO_FULL, 30)
        s_minwick = self._scale(min_wick, self.MIN_WICK_ZERO, self.MIN_WICK_FULL, 30)
        total = s_body + s_wick + s_minwick
        return total, s_body, s_wick, s_minwick, 0, 0, 0, 0, 0, 0

    @staticmethod
    def _scale(value, lo, hi, max_score):
        """线性映射：value<=lo → 0, value>=hi → max_score。"""
        if hi <= lo:
            return 0
        ratio = max(0.0, min(1.0, (value - lo) / (hi - lo)))
        return int(max_score * ratio)

    @staticmethod
    def _scale_inv(value, lo, hi, max_score):
        """反向线性映射：value<=lo → max_score, value>=hi → 0。"""
        if hi <= lo:
            return 0
        ratio = max(0.0, min(1.0, (hi - value) / (hi - lo)))
        return int(max_score * ratio)

    @staticmethod
    def _state_from_score(score: int) -> str:
        # 阈值按 600760 标准答案拟合：答案 96-100，假阳性≤83，故 90/75/55 三档切开
        if score >= 90:
            return "INSTITUTIONAL"   # 标准答案区（吸筹确认）
        if score >= 75:
            return "ACCUMULATE"      # 接近但未达（含部分假阳性）
        if score >= 55:
            return "SUSPECT"         # 可疑
        return "NORMAL"

    @staticmethod
    def _normalize_range(start, end) -> Tuple[datetime, datetime]:
        def _to_date(x):
            if isinstance(x, datetime):
                return x.date()
            if isinstance(x, date):
                return x
            s = str(x).strip()
            fmt = "%Y%m%d" if "-" not in s else "%Y-%m-%d"
            return datetime.strptime(s, fmt).date()
        s = _to_date(start)
        e = _to_date(end)
        return datetime(s.year, s.month, s.day), datetime(e.year, e.month, e.day)


def format_results(results: List[AccumulationResult]) -> str:
    """格式化为控制台表格。

    明细列 = 体/影/双（十字星超短线识别 100 分）：
      体 实体占价格% (40，越小越好)   影 影线占比 (30，越大越好)
      双 双侧影线门槛 (30，min(上,下)影越大越好)
    开/收/实体/大单%/连续 仅作参考列展示。
    """
    if not results:
        return "无吸筹候选"

    lines = []
    lines.append(
        f"{'日期':<12}{'评分':>4}  {'明细(体/影/衰)':<16}{'状态':<14}"
        f"{'开盘':>7}{'收盘':>7}{'实体%':>7}{'影线%':>6}{'振幅%':>6}"
        f"{'大单%':>8}{'连续':>4}"
    )
    lines.append("-" * 110)
    for r in results:
        breakdown = f"{r.score_pos}/{r.score_decay}/{r.score_consec}"
        # 振幅用高低价差/均价近似（n_bins*bin_size 不准，用 price_ref）
        lines.append(
            f"{str(r.date):<12}"
            f"{r.score:>4}  "
            f"{breakdown:<16}"
            f"{r.state:<14}"
            f"{r.open_price:>7.2f}"
            f"{(r.open_price + r.close_vs_avg/100*r.price_ref):>7.2f}"
            f"{r.body_pct:>6.2f}%"
            f"{r.wick_ratio:>5.0f}%"
            f"{(r.bin_size * r.n_bins / r.price_ref * 100):>5.1f}%"
            f"{r.big_pct:>+7.2f}%"
            f"{r.big_consecutive:>4}"
        )
    return "\n".join(lines)
