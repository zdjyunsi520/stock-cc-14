# -*- coding: utf-8 -*-
"""全市场日线历史数据同步服务 — Baostock 主力源。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board, is_excluded_by_name

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    total: int = 0
    synced: int = 0
    skipped: int = 0
    failed: int = 0
    rows_written: int = 0
    errors: List[str] = field(default_factory=list)


class DailyDataSyncService:
    """将全市场非ST A股日线历史数据同步到本地 stock_daily 表。"""

    def __init__(
        self,
        *,
        config: Any = None,
        db: Optional[DatabaseManager] = None,
        interval_seconds: float = 3.0,
        start_date: str = "2000-01-01",
    ) -> None:
        if config is None:
            from src.config import get_config
            config = get_config()
        self.config = config
        self.db = db or DatabaseManager()
        self.interval = max(0.1, float(interval_seconds))
        self.start_date = start_date

    # ------------------------------------------------------------------
    # Stock list
    # ------------------------------------------------------------------

    def get_stock_list(self, bs=None, *, exclude_st: bool = True, only_listed: bool = True) -> List[Dict[str, str]]:
        """获取非ST、非退市股票列表。bs=None 时自动管理 session。"""
        import baostock as _bs

        own_session = bs is None
        if own_session:
            bs = _bs
            lg = bs.login()
            if lg.error_code != "0":
                logger.warning("[DailySync] Baostock login 失败: %s", lg.error_msg)
                return []

        try:
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                logger.warning("[DailySync] query_stock_basic 失败: %s", rs.error_msg)
                return []

            rows_data = []
            while rs.next():
                rows_data.append(rs.get_row_data())

            if not rows_data:
                return []

            df = pd.DataFrame(rows_data, columns=rs.fields)
        finally:
            if own_session:
                bs.logout()

        # 转换代码格式
        df["code"] = df["code"].apply(lambda x: x.split(".")[1] if "." in str(x) else x)
        df = df.rename(columns={"code_name": "name"})

        stocks: List[Dict[str, str]] = []
        for _, row in df.iterrows():
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            stock_type = str(row.get("type", ""))
            status = str(row.get("status", ""))
            out_date = str(row.get("outDate", "")).strip()

            if not code:
                continue
            # 只保留 A 股 (type=1)
            if stock_type != "1":
                continue
            # 排除退市（outDate 非空表示已退市）
            if only_listed and out_date and out_date not in ("", "None", "nan"):
                continue
            # 板块黑名单：300/301 创业板、688 科创板、4/8 北交所
            if is_excluded_board(code):
                continue
            # ST/*ST/退市（与 exclude_st 参数解耦：黑名单始终生效）
            if is_excluded_by_name(name):
                continue
            # 兼容旧 exclude_st 语义：如调用方明确不排除 ST，回滚到只看板块
            # （当前无调用方传 exclude_st=False，但保留钩子避免破坏 API）
            if exclude_st and not is_excluded_by_name(name):
                pass  # no-op，保留参数以兼容
            stocks.append({"code": code, "name": name})

        logger.info("[DailySync] 股票列表: %d 只（排除ST=%s, 排除退市=%s）", len(stocks), exclude_st, only_listed)
        return stocks

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync_full(
        self,
        *,
        max_stocks: Optional[int] = None,
        batch_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> SyncResult:
        """全量同步，支持断点续传。batch_callback(完成数, 总数, 当前代码)。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            return SyncResult(errors=[f"Baostock login failed: {lg.error_msg}"])

        try:
            stocks = self.get_stock_list(bs)
        except Exception:
            bs.logout()
            return SyncResult()

        if not stocks:
            bs.logout()
            return SyncResult()

        if max_stocks and max_stocks > 0:
            stocks = stocks[:max_stocks]

        # 加载已完成的 sync_state，用于断点续传
        done_codes = set()
        for state in self.db.get_sync_states():
            if state["status"] == "done":
                done_codes.add(state["code"])

        result = SyncResult(total=len(stocks))
        self._init_sync_states(stocks, done_codes)
        consecutive_failures = 0

        try:
            for idx, stock in enumerate(stocks):
                code = stock["code"]
                name = stock["name"]

                if code in done_codes:
                    result.skipped += 1
                    if batch_callback:
                        batch_callback(idx + 1, result.total, code)
                    continue

                self.db.upsert_sync_state(code, name, status="syncing")
                try:
                    rows = self._sync_one_stock(bs, code, name)
                    result.synced += 1
                    result.rows_written += rows
                    if rows > 0:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                    logger.info("[DailySync] %s %s: %d行写入 (进度 %d/%d)", code, name, rows, idx + 1, len(stocks))
                except Exception as exc:
                    consecutive_failures += 1
                    result.failed += 1
                    result.errors.append(f"{code}: {exc}")
                    self.db.upsert_sync_state(code, name, status="failed", error_message=str(exc)[:500])
                    logger.warning("[DailySync] %s %s 失败: %s", code, name, exc)

                    # 连续失败 3 次，尝试 re-login 重连
                    if consecutive_failures >= 3:
                        logger.warning("[DailySync] 连续 %d 次失败，尝试 re-login", consecutive_failures)
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        import time as _t
                        _t.sleep(5)
                        lg2 = bs.login()
                        if lg2.error_code == "0":
                            consecutive_failures = 0
                            logger.info("[DailySync] re-login 成功，继续同步")
                        else:
                            logger.error("[DailySync] re-login 失败: %s", lg2.error_msg)

                # 连续10只无数据，数据源未就绪，停止
                if consecutive_failures >= 10:
                    logger.warning(
                        "[DailySync] 连续 %d 只无数据，数据源可能未更新，停止同步",
                        consecutive_failures,
                    )
                    break

                if batch_callback:
                    batch_callback(idx + 1, result.total, code)

                processed = idx + 1
                if processed % 50 == 0 or processed == len(stocks):
                    logger.info(
                        "[DailySync] 进度 %d/%d（%.0f%%） synced=%d skipped=%d failed=%d rows=%d 当前=%s %s",
                        processed, len(stocks), processed / len(stocks) * 100,
                        result.synced, result.skipped, result.failed, result.rows_written,
                        code, name,
                    )

                if idx < len(stocks) - 1:
                    time.sleep(self.interval)

        finally:
            bs.logout()

        logger.info(
            "[DailySync] 全量同步完成: total=%d synced=%d skipped=%d failed=%d rows=%d",
            result.total, result.synced, result.skipped, result.failed, result.rows_written,
        )
        return result

    def _sync_one_stock(self, bs, code: str, name: str) -> int:
        """同步单只股票最近120天数据，返回写入行数。"""
        bs_code = self._to_bs_code(code)
        target = self._last_complete_trading_date()
        start = (target - timedelta(days=120)).strftime("%Y-%m-%d")
        end = target.strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            code=bs_code,
            fields="date,open,high,low,close,volume,amount,pctChg",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )

        # Baostock 连接可能中途断开，检查 error_code
        if rs.error_code != "0":
            raise RuntimeError(f"Baostock error: {rs.error_code} {rs.error_msg}") from None

        rows_data = []
        while rs.next():
            rows_data.append(rs.get_row_data())

        if not rows_data:
            # 确实无数据（停牌/新股未上市），标记 done
            self.db.upsert_sync_state(code, name, status="done", total_days=0)
            return 0

        df = pd.DataFrame(rows_data, columns=rs.fields)
        if "pctChg" in df.columns:
            df = df.rename(columns={"pctChg": "pct_chg"})

        for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        written = self.db.save_daily_data_bulk(df, code, data_source="BaostockSync")

        last_date_str = str(df["date"].iloc[-1]) if "date" in df.columns and not df.empty else None
        last_date_obj = self._parse_date(last_date_str) if last_date_str else None
        self.db.upsert_sync_state(
            code, name,
            last_synced_date=last_date_obj,
            total_days=len(df),
            status="done",
        )
        return written

    # ------------------------------------------------------------------
    # Incremental sync
    # ------------------------------------------------------------------

    def sync_incremental(self, *, codes: Optional[List[str]] = None) -> SyncResult:
        """增量同步已完成的股票，只拉取 last_synced_date 之后的数据。"""
        states = self.db.get_sync_states(status="done")
        if codes:
            code_set = set(codes)
            states = [s for s in states if s["code"] in code_set]

        if not states:
            logger.info("[DailySync] 无已完成股票可增量同步")
            return SyncResult()

        result = SyncResult(total=len(states))
        target_date = self._last_complete_trading_date()

        import baostock as bs

        login_result = bs.login()
        if login_result.error_code != "0":
            result.errors.append(f"Baostock login failed: {login_result.error_msg}")
            return result

        try:
            consecutive_empty = 0
            for idx, state in enumerate(states):
                code = state["code"]
                name = state.get("code_name", "")
                last_date = state.get("last_synced_date")

                start = self._next_trading_day(last_date) if last_date else target_date - timedelta(days=120)
                start_str = start.strftime("%Y-%m-%d") if isinstance(start, date) else str(start)
                end_str = target_date.strftime("%Y-%m-%d")

                if start_str > end_str:
                    result.skipped += 1
                    continue

                try:
                    rows = self._sync_one_stock_incremental(bs, code, name, start_str, end_str)
                    if rows == 0:
                        # 0行疑似限流：暂停60秒冷却，重连后重试本股
                        logger.warning("[DailySync] %s %s 返回0行，60秒冷却后重连重试", code, name)
                        rows, retry_exc = self._retry_after_reconnect(
                            bs, code, name, start_str, end_str, 60,
                        )
                        if retry_exc:
                            raise retry_exc
                        logger.info("[DailySync] %s %s 重连后: %d行", code, name, rows)
                    result.synced += 1
                    result.rows_written += rows
                    if rows > 0:
                        consecutive_empty = 0
                    else:
                        consecutive_empty += 1
                    logger.info("[DailySync] 增量 %s %s: %d行 (进度 %d/%d)", code, name, rows, idx + 1, len(states))
                except Exception as exc:
                    exc_msg = str(exc)
                    logger.warning("[DailySync] 增量 %s 失败: %s", code, exc)

                    # 10053/接收数据异常/软件中止：限流征兆（与0行同类），60秒冷却后重连重试本股
                    if ("10053" in exc_msg or "接收数据异常" in exc_msg or "软件中止" in exc_msg):
                        logger.warning("[DailySync] %s %s 连接异常，60秒冷却后重连重试该股", code, name)
                        rows, retry_exc = self._retry_after_reconnect(
                            bs, code, name, start_str, end_str, 60,
                        )
                        if retry_exc:
                            result.failed += 1
                            consecutive_empty += 1
                            result.errors.append(f"{code}: {exc} | 重连后 {retry_exc}")
                            logger.warning("[DailySync] %s 重连后仍失败: %s", code, retry_exc)
                        else:
                            result.synced += 1
                            result.rows_written += rows
                            if rows > 0:
                                consecutive_empty = 0
                            else:
                                consecutive_empty += 1
                            logger.info("[DailySync] %s %s 重连后: %d行", code, name, rows)
                    # 10054类（远程关闭/forcibly/reset）：10分钟重连继续下一只
                    elif ("10054" in exc_msg
                            or "forcibly" in exc_msg.lower()
                            or "reset" in exc_msg.lower()
                            or "ConnectionReset" in exc_msg):
                        result.failed += 1
                        consecutive_empty += 1
                        result.errors.append(f"{code}: {exc}")
                        logger.warning("[DailySync] 检测到连接断开，等待 600 秒后重连...")
                        time.sleep(600)
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        login_result = bs.login()
                        if login_result.error_code != "0":
                            logger.error("[DailySync] 重连失败: %s，停止同步", login_result.error_msg)
                            break
                        logger.info("[DailySync] 重连成功，继续同步")
                    # 其他异常：只计 failed
                    else:
                        result.failed += 1
                        consecutive_empty += 1
                        result.errors.append(f"{code}: {exc}")

                # 连续10只无数据，数据源未就绪，停止
                if consecutive_empty >= 10:
                    logger.warning(
                        "[DailySync] 连续 %d 只无数据，数据源可能未更新（通常收盘后15:30可用），停止同步",
                        consecutive_empty,
                    )
                    break

                time.sleep(self.interval)

        finally:
            bs.logout()

        logger.info(
            "[DailySync] 增量同步完成: total=%d synced=%d failed=%d rows=%d",
            result.total, result.synced, result.failed, result.rows_written,
        )
        return result

    def _retry_after_reconnect(self, bs, code: str, name: str,
                               start_str: str, end_str: str,
                               wait_seconds: int) -> Tuple[int, Optional[Exception]]:
        """等待 + 重连 + 重试本股一次。

        0行与10053限流路径共用。

        Returns:
            (rows, exc): 重连失败或重试异常时 exc 不为 None；否则 rows 为重试结果（可能仍0）。
        """
        logger.info("[DailySync] 等待 %d 秒后重连", wait_seconds)
        time.sleep(wait_seconds)
        try:
            bs.logout()
        except Exception:
            pass
        login_result = bs.login()
        if login_result.error_code != "0":
            return 0, RuntimeError(f"重连失败: {login_result.error_msg}")
        try:
            rows = self._sync_one_stock_incremental(bs, code, name, start_str, end_str)
            return rows, None
        except Exception as exc:
            return 0, exc

    def _sync_one_stock_incremental(self, bs, code: str, name: str,
                                     start_str: str, end_str: str) -> int:
        bs_code = self._to_bs_code(code)
        rs = bs.query_history_k_data_plus(
            code=bs_code,
            fields="date,open,high,low,close,volume,amount,pctChg",
            start_date=start_str,
            end_date=end_str,
            frequency="d",
            adjustflag="2",
        )

        rows_data = []
        while rs.next():
            rows_data.append(rs.get_row_data())

        if not rows_data:
            return 0

        df = pd.DataFrame(rows_data, columns=rs.fields)
        if "pctChg" in df.columns:
            df = df.rename(columns={"pctChg": "pct_chg"})
        for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        written = self.db.save_daily_data_bulk(df, code, data_source="BaostockSync")
        last_date_str = str(df["date"].iloc[-1]) if "date" in df.columns and not df.empty else None
        last_date_obj = self._parse_date(last_date_str) if last_date_str else None
        self.db.upsert_sync_state(
            code, name,
            last_synced_date=last_date_obj,
            total_days=len(df),
            status="done",
        )
        return written

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def get_sync_progress(self) -> Dict[str, Any]:
        states = self.db.get_sync_states()
        done = sum(1 for s in states if s["status"] == "done")
        failed = sum(1 for s in states if s["status"] == "failed")
        syncing = sum(1 for s in states if s["status"] == "syncing")
        pending = sum(1 for s in states if s["status"] == "pending")
        total_rows = sum(s.get("total_days", 0) or 0 for s in states)
        return {
            "total_stocks": len(states),
            "done": done,
            "failed": failed,
            "syncing": syncing,
            "pending": pending,
            "total_rows": total_rows,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_sync_states(self, stocks: List[Dict[str, str]], done_codes: set) -> None:
        """初始化尚未录入 sync_state 的股票为 pending。"""
        existing = {s["code"] for s in self.db.get_sync_states()}
        for stock in stocks:
            if stock["code"] not in existing and stock["code"] not in done_codes:
                self.db.upsert_sync_state(
                    stock["code"], stock["name"], status="pending",
                )

    @staticmethod
    def _to_bs_code(code: str) -> str:
        if "." in code:
            return code
        prefix = "sh" if code.startswith("6") else "sz"
        return f"{prefix}.{code}"

    @staticmethod
    def _last_complete_trading_date() -> date:
        """计算最近一个数据应已完备的交易日。

        规则:
        - 工作日 15:30 之后 → 当天
        - 工作日 15:30 之前 → 前一个交易日
        - 周六 → 周五
        - 周日 → 周五
        """
        from datetime import datetime as _dt
        now = _dt.now()
        today = now.date()
        weekday = today.weekday()  # 0=Mon ... 6=Sun

        if weekday == 5:  # 周六
            return today - timedelta(days=1)
        if weekday == 6:  # 周日
            return today - timedelta(days=2)

        # 工作日
        if now.hour >= 16 or (now.hour == 15 and now.minute >= 30):
            return today
        # 15:30 前，回退到上一个交易日
        offset = 3 if weekday == 0 else 1  # 周一回退到上周五
        return today - timedelta(days=offset)

    @staticmethod
    def _next_trading_day(d) -> Optional[date]:
        if d is None:
            return None
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                return None
        if isinstance(d, datetime):
            d = d.date()
        return d + timedelta(days=1)

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        try:
            return date.fromisoformat(str(value).strip())
        except (ValueError, AttributeError):
            return None
