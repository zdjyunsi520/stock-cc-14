# -*- coding: utf-8 -*-
"""跨股票异常时段聚类器（"一波集体吸筹"识别）。

思路：
1. 用 AnomalyPeriodDetector 扫描所有股票 → 得到全部时段
2. 按时段 start_date 聚类（相邻 ≤ cluster_gap 天视为同一波）
3. 每个聚类查 stock_concept_membership 反推热门概念

用法:
    clusterer = AnomalyClusterer()
    clusters = clusterer.run(max_gap=2, cluster_gap=3, min_codes=3)
    print(format_clusters(clusters))
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

from src.storage import DatabaseManager
from src.services.anomaly_period_detector import (
    AnomalyPeriod, AnomalyPeriodDetector,
)

logger = logging.getLogger(__name__)


@dataclass
class AnomalyCluster:
    """跨股票的异常时段聚类。"""
    start_date: date                              # 聚类内最早 start_date
    end_date: date                                # 聚类内最晚 start_date
    span_days: int                                # 自然日跨度
    code_count: int                               # 涉及股票数
    period_count: int                             # 总时段数（一只股可能有多个）
    total_big_net: float                          # 大单累计（万）
    total_small_net: float                        # 小单累计（万）
    avg_price_pct: float                          # 加权平均区间涨幅%
    codes: List[str] = field(default_factory=list)
    top_concepts: List[Tuple[str, str, int]] = field(default_factory=list)


class AnomalyClusterer:
    """全量扫描 + 时段聚类 + 概念反推。"""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()
        self.detector = AnomalyPeriodDetector(db=self.db)

    def run(
        self,
        max_gap: int = 2,
        cluster_gap: int = 2,
        max_span_days: int = 7,
        min_codes: int = 10,
        top_concepts: int = 5,
        progress_every: int = 500,
    ) -> List[AnomalyCluster]:
        """完整流程：扫描全量 → 时段聚类 → 反推概念。

        Args:
            max_gap: 单股时段内允许的最大连续未触发天数（透传给 detector）
            cluster_gap: 跨股票时段 start_date 局部相邻阈值（≤ N 天视为连续）
            max_span_days: 单个聚类最大自然日跨度（强制拆分，防止传递闭包过大）
            min_codes: 聚类最少股票数（过滤单股噪音）
            top_concepts: 每个聚类保留的概念数
            progress_every: 每处理 N 只股票打印一次进度

        Returns:
            List[AnomalyCluster]，按 start_date 升序
        """
        periods = self.scan_all(max_gap=max_gap, progress_every=progress_every)
        logger.info(
            "扫描完成: %d 只股票出现异常, 共 %d 个时段",
            len({p.code for p in periods}), len(periods),
        )

        clusters = self.cluster(
            periods,
            cluster_gap=cluster_gap,
            max_span_days=max_span_days,
            min_codes=min_codes,
        )
        logger.info("聚类完成: %d 个有效聚类 (≥%d 只股票)", len(clusters), min_codes)

        for c in clusters:
            c.top_concepts = self.lookup_concepts(c.codes, top=top_concepts)
        return clusters

    def scan_all(self, max_gap: int = 2, progress_every: int = 500) -> List[AnomalyPeriod]:
        """全量扫描所有有资金流数据的股票。"""
        codes = self._load_all_codes()
        logger.info("待扫描股票: %d 只", len(codes))

        all_periods: List[AnomalyPeriod] = []
        hit_codes = 0
        for i, code in enumerate(codes):
            try:
                ps = self.detector.detect(code, max_gap=max_gap)
                if ps:
                    all_periods.extend(ps)
                    hit_codes += 1
            except Exception as e:
                logger.warning("detect %s 失败: %s", code, e)
            if progress_every and (i + 1) % progress_every == 0:
                logger.info(
                    "进度 %d/%d, 命中 %d 只, 累积 %d 个时段",
                    i + 1, len(codes), hit_codes, len(all_periods),
                )
        return all_periods

    def cluster(
        self,
        periods: List[AnomalyPeriod],
        cluster_gap: int = 2,
        max_span_days: int = 7,
        min_codes: int = 10,
    ) -> List[AnomalyCluster]:
        """按时段 start_date 聚类（局部相邻 + 全局跨度上限）。

        - 局部相邻：相邻时段 start_date 差 ≤ cluster_gap 视为连续
        - 全局跨度：当前聚类最早 start_date 到新时段 start_date 超 max_span_days 强制拆分
        （防止传递闭包把全部股票串成一类）
        """
        if not periods:
            return []

        sorted_p = sorted(periods, key=lambda p: p.start_date)

        clusters: List[AnomalyCluster] = []
        current: List[AnomalyPeriod] = [sorted_p[0]]
        for k in range(1, len(sorted_p)):
            prev_start = sorted_p[k - 1].start_date
            cur_start = sorted_p[k].start_date
            cluster_start = current[0].start_date
            locally_adjacent = (cur_start - prev_start).days <= cluster_gap
            within_span = (cur_start - cluster_start).days <= max_span_days
            if locally_adjacent and within_span:
                current.append(sorted_p[k])
            else:
                clusters.append(self._build_cluster(current))
                current = [sorted_p[k]]
        clusters.append(self._build_cluster(current))

        return [c for c in clusters if c.code_count >= min_codes]

    @staticmethod
    def _build_cluster(members: List[AnomalyPeriod]) -> AnomalyCluster:
        codes = list({p.code for p in members})
        big = sum(p.total_big_net for p in members)
        small = sum(p.total_small_net for p in members)
        # 按 span_days 加权平均涨幅
        total_w = sum(p.span_days for p in members) or 1
        avg_pct = sum(p.price_pct * p.span_days for p in members) / total_w

        start = min(p.start_date for p in members)
        end = max(p.start_date for p in members)
        return AnomalyCluster(
            start_date=start,
            end_date=end,
            span_days=(end - start).days + 1 if start and end else 0,
            code_count=len(codes),
            period_count=len(members),
            total_big_net=round(big, 0),
            total_small_net=round(small, 0),
            avg_price_pct=round(avg_pct, 2),
            codes=codes,
        )

    def lookup_concepts(
        self,
        codes: List[str],
        top: int = 5,
    ) -> List[Tuple[str, str, int]]:
        """查 stock_concept_membership 反推热门概念。

        Returns:
            [(concept_code, concept_name, hit_count), ...]，按 hit_count 降序
        """
        if not codes:
            return []
        from src.storage import StockConceptMembership
        session = self.db.get_session()
        try:
            # SQLite IN 上限 999，分批查
            counter: dict = defaultdict(int)
            names: dict = {}
            batch_size = 900
            for i in range(0, len(codes), batch_size):
                batch = codes[i:i + batch_size]
                rows = session.query(
                    StockConceptMembership.concept_code,
                    StockConceptMembership.concept_name,
                ).filter(
                    StockConceptMembership.code.in_(batch)
                ).all()
                for cc, cn in rows:
                    counter[cc] += 1
                    names[cc] = cn
            sorted_c = sorted(counter.items(), key=lambda kv: -kv[1])[:top]
            return [(cc, names[cc], cnt) for cc, cnt in sorted_c]
        finally:
            session.close()

    def _load_all_codes(self) -> List[str]:
        """加载所有有资金流数据的股票代码。"""
        from src.storage import StockFundFlow
        session = self.db.get_session()
        try:
            rows = session.query(StockFundFlow.code).distinct().all()
            return [r[0] for r in rows]
        finally:
            session.close()


def format_clusters(clusters: List[AnomalyCluster]) -> str:
    """格式化聚类列表。"""
    if not clusters:
        return "未发现集体吸筹聚类（同时间窗内股票数 < min_codes）"

    lines = [f"发现 {len(clusters)} 个集体吸筹聚类:", "=" * 110]
    for i, c in enumerate(clusters, 1):
        lines.append(
            f"#{i}  {c.start_date} ~ {c.end_date} ({c.span_days}天)  "
            f"股票 {c.code_count}只 / 时段 {c.period_count}个  "
            f"大单累计 {c.total_big_net/1e4:+.2f}亿 / 小单 {c.total_small_net/1e4:+.2f}亿 / "
            f"加权涨幅 {c.avg_price_pct:+.2f}%"
        )
        if c.top_concepts:
            conc = "  ".join(f"{n}({cnt})" for _, n, cnt in c.top_concepts)
            lines.append(f"    热门概念: {conc}")
        if c.code_count <= 20:
            lines.append(f"    涉及股票: {', '.join(c.codes)}")
        lines.append("-" * 110)
    return "\n".join(lines)
