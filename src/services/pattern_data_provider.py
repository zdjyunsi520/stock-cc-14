# -*- coding: utf-8 -*-
"""规律选股数据获取协调器：缓存优先 + 实时回退 + 显式日志。

三步数据保障：
- 概念：由 PatternScreener._load_theme_universe 自带缓存+fallback，本类不介入
- 日线：复用 DailyDataSyncService.sync_incremental(codes=...) 同步指定股票
        （baostock 源，即原 run_sync_incremental 的实现，自带限流/重连）
- 资金流：检查 stock_fund_flow 覆盖度，缺失才调 fetch_fund_flow（保留 3s 限流）
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Tuple

from sqlalchemy import text

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


class PatternDataProvider:
    """规律选股数据获取协调器。"""

    def __init__(self, db: DatabaseManager | None = None):
        self.db = db or DatabaseManager()

    # ============== 日线 ==============
    def ensure_daily(
        self,
        codes: List[str],
        end_date: str = "",
        lookback_days: int = 30,
    ) -> Dict[str, int]:
        """复用 DailyDataSyncService.sync_incremental 同步指定 codes 的日线。

        即原 --sync-incremental 的实现（baostock 源），仅把入参改为限定 codes。
        内部自动处理：限流(0 行/10053/10054 等)重连、interval 间隔、写入 stock_daily。
        注意：codes 必须已在 stock_daily_sync_state 表中 status=done 才会被处理；
        未在 sync_state 的 code 会静默跳过（PatternScreener 后续读 stock_daily
        时若数据不足会自然跳过该股）。

        Args:
            codes: 指定股票代码列表
            end_date: 保留参数（兼容调用方），sync_incremental 内部自动用
                     last_synced_date → 最近交易日
            lookback_days: 保留参数（兼容调用方），sync_incremental 内部自动覆盖

        Returns:
            {"total": 处理数, "synced": 成功数, "failed": 失败数,
             "rows_written": 入库行数, "skipped": 跳过数}
        """
        from src.services.daily_data_sync_service import DailyDataSyncService

        logger.info(
            "[DataProvider] 日线同步开始: %d 只 codes 范围 (baostock 增量)",
            len(codes),
        )
        try:
            svc = DailyDataSyncService()
            result = svc.sync_incremental(codes=codes)
            logger.info(
                "[DataProvider] 日线同步完成: total=%d synced=%d failed=%d "
                "rows=%d skipped=%d (候选 %d 只, 未在 sync_state 中的 %d 只未处理)",
                result.total, result.synced, result.failed,
                result.rows_written, result.skipped,
                len(codes), len(codes) - result.total,
            )
            return {
                "total": result.total,
                "synced": result.synced,
                "failed": result.failed,
                "rows_written": result.rows_written,
                "skipped": result.skipped,
            }
        except Exception as exc:
            logger.exception("[DataProvider] 日线同步整体失败: %s", exc)
            return {
                "total": 0,
                "synced": 0,
                "failed": len(codes),
                "rows_written": 0,
                "skipped": 0,
            }

    # ============== 资金流 ==============
    def ensure_fund_flow(
        self,
        picked_codes: List[Tuple[str, str]],
        end_date: str,
        min_rows: int = 10,
    ) -> Dict[str, int]:
        """检查 picked 候选股 stock_fund_flow 覆盖度，缺失才实时抓取。

        Args:
            picked_codes: [(code, name), ...]
            end_date: YYYYMMDD 截止日
            min_rows: FundFlowScreener._analyze_one 要求 ≥10 行

        Returns:
            {code: 入库行数}；-1 失败；0 已充足跳过
        """
        import requests
        from scripts.scrape_fund_flow import fetch_fund_flow, save_fund_flow

        report: Dict[str, int] = {}
        sess = requests.Session()
        end_iso = datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d")

        logger.info(
            "[DataProvider] 资金流检查开始: %d 只, 截止 %s",
            len(picked_codes), end_iso,
        )
        total = len(picked_codes)
        for i, (code, name) in enumerate(picked_codes, 1):
            existing, latest = self._check_fund_flow(code)
            # 充足判定：行数够 + 最新日期到达 end_date
            if existing >= min_rows and latest >= end_iso:
                logger.info(
                    "[DataProvider] [%d/%d] %s 资金流充足(%d行,最新%s), 跳过",
                    i, total, code, existing, latest,
                )
                report[code] = 0
                continue

            logger.info(
                "[DataProvider] [%d/%d] %s 资金流不足(%d行/<%d或最新%s<%s), 抓取",
                i, total, code, existing, min_rows, latest, end_iso,
            )
            try:
                rows = fetch_fund_flow(code, session=sess)
                if not rows:
                    logger.warning(
                        "[DataProvider] %s 资金流抓取返回空, 跳过", code,
                    )
                    report[code] = -1
                    continue
                saved = save_fund_flow(self.db, rows)
                logger.info(
                    "[DataProvider] %s 资金流抓取成功, 入库 %d 行", code, saved,
                )
                report[code] = saved
            except Exception as exc:
                logger.warning(
                    "[DataProvider] %s 资金流抓取失败: %s", code, exc,
                )
                report[code] = -1
            if i < total:
                time.sleep(3.0)  # 与 scrape_fund_flow.scrape_codes 默认 interval 一致

        logger.info("[DataProvider] 资金流检查完成")
        return report

    def _check_fund_flow(self, code: str) -> Tuple[int, str]:
        """返回 (现有行数, 最新日期 YYYY-MM-DD)。"""
        with self.db.get_session() as session:
            r = session.execute(
                text(
                    "SELECT COUNT(*) AS cnt, MAX(date) AS latest "
                    "FROM stock_fund_flow WHERE code=:c"
                ),
                {"c": code},
            )
            row = r.one()
            return int(row.cnt or 0), str(row.latest or "")
