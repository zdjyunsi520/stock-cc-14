# -*- coding: utf-8 -*-
"""统一的交易日工具类。

提供：
- get_last_trading_date() - 获取上一个交易日（适用于 15:30 前运行）
- get_current_trading_date() - 获取当前交易日（15:30 后返回今天，否则返回上一个交易日）
- is_trading_day(date) - 判断是否为交易日
- format_date(date) - 格式化日期为 YYYYMMDD
"""

from datetime import date, datetime, time, timedelta


class TradingDate:
    """统一的交易日工具类。"""

    @staticmethod
    def get_last_trading_date(target_date: date | None = None) -> date:
        """获取上一个交易日。

        Args:
            target_date: 参考日期，None 则使用今天

        Returns:
            上一个交易日的日期

        规则:
        - 周一 → 上周五
        - 其他工作日 → 前一天
        """
        d = target_date or date.today()
        weekday = d.weekday()  # 0=Mon ... 6=Sun

        if weekday == 0:  # 周一
            return d - timedelta(days=3)  # 回退到上周五
        elif weekday == 6:  # 周日
            return d - timedelta(days=2)  # 回退到周五
        elif weekday == 5:  # 周六
            return d - timedelta(days=1)  # 回退到周五
        else:  # 周二到周五
            return d - timedelta(days=1)

    @staticmethod
    def get_current_trading_date(refresh_time: bool = False) -> date:
        """获取当前交易日（考虑时间）。

        Args:
            refresh_time: 是否强制刷新时间判断

        Returns:
            当前交易日的日期

        规则:
        - 工作日 15:30 之后 → 当天
        - 工作日 15:30 之前 → 上一个交易日
        - 周六 → 周五
        - 周日 → 周五
        """
        now = datetime.now()
        today = now.date()
        weekday = today.weekday()  # 0=Mon ... 6=Sun

        # 周末
        if weekday == 5:  # 周六
            return today - timedelta(days=1)
        if weekday == 6:  # 周日
            return today - timedelta(days=2)

        # 工作日：检查时间
        cutoff = time(15, 30)  # 15:30
        current_time = now.time()

        if current_time >= cutoff:
            return today
        else:
            # 15:30 前，返回上一个交易日
            return TradingDate.get_last_trading_date(today)

    @staticmethod
    def is_trading_day(d: date) -> bool:
        """判断是否为交易日。

        Args:
            d: 要判断的日期

        Returns:
            True 为交易日，False 为非交易日（周末）

        注意：
        - 只排除了周六日
        - 未考虑节假日（如春节、国庆等）
        """
        return d.weekday() < 5  # 0-4 为周一到周五

    @staticmethod
    def format_date(d: date) -> str:
        """格式化日期为 YYYYMMDD。"""
        return d.strftime("%Y%m%d")

    @staticmethod
    def get_last_complete_trading_date() -> date:
        """获取最近一个数据应已完备的交易日。

        与 DailyDataSyncService._last_complete_trading_date() 保持一致，
        但统一到这个工具类中便于维护。

        规则:
        - 工作日 15:30 之后 → 当天
        - 工作日 15:30 之前 → 前一个交易日
        - 周六 → 周五
        - 周日 → 周五
        """
        return TradingDate.get_current_trading_date()

    @staticmethod
    def get_target_trade_date(session) -> date:
        """基于 stock_daily 表确定目标交易日（自动跳过节假日）。

        规则（用户指定）:
        - 15:30 后 + 工作日 + today >= stock_daily.MAX(date) → today
        - 其他情况 → stock_daily.MAX(date)（最近的真实已收盘交易日）

        相比 get_current_trading_date()，本方法依赖 stock_daily 表里的真实
        交易日历（已经从 tushare 同步过），自动跳过周末和已知节假日。
        边界：如果今天是新的节假日（stock_daily 还没同步），仍会按工作日处理。

        Args:
            session: SQLAlchemy session（用于查询 stock_daily）

        Returns:
            目标交易日 date 对象

        Raises:
            RuntimeError: stock_daily 无数据时
        """
        from sqlalchemy import text
        today = date.today()
        now = datetime.now()

        row = session.execute(text("SELECT MAX(date) FROM stock_daily")).fetchone()
        if not row or not row[0]:
            raise RuntimeError("stock_daily 表无数据，无法确定交易日")
        max_d = row[0]
        # stock_daily.date 列虽声明为 DATE，但 SQLite 实际存为 TEXT（'YYYY-MM-DD'）。
        # raw SQL 经 text() 执行不会自动转型，这里统一转成 date 对象。
        if isinstance(max_d, str):
            max_d = date.fromisoformat(max_d[:10])
        elif isinstance(max_d, datetime):
            max_d = max_d.date()

        if (now.time() >= time(15, 30)
                and today.weekday() < 5
                and today >= max_d):
            return today
        return max_d
