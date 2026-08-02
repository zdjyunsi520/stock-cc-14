# -*- coding: utf-8 -*-
"""盘中选股临时库管理（仅日线，无资金流）。

完全独立于 DatabaseManager（避免单例缓存污染）。
- build_intraday_db: 删旧库 + 建空表（用完即废）
- copy_history_from_production: 从 stock_daily 拷历史日线到 tmp 库

注意：根据 --wash-v3 等效需求，盘中选股不处理资金流数据。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import List

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


SCHEMA_DAILY = """
CREATE TABLE stock_daily_intraday_tmp (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL,
    PRIMARY KEY (code, date)
)
"""


def build_intraday_db(db_path: str = "data/intraday_screen.db") -> Engine:
    """每次启动先删旧库 + 建空表（用完即废）。

    Args:
        db_path: 临时库文件路径

    Returns:
        SQLAlchemy Engine（指向全新的 tmp 库）
    """
    parent = os.path.dirname(db_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            logger.info("[IntradayDB] 删除旧 tmp 库: %s", db_path)
        except OSError as exc:
            logger.warning("[IntradayDB] 删除旧库失败 %s: %s", db_path, exc)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_DAILY))
    logger.info("[IntradayDB] 新 tmp 库已建: %s", db_path)
    return engine


def _normalize_end_date(end_date: str | date | None) -> date:
    """统一 end_date 参数为 date 对象。None 取今天。"""
    if end_date is None:
        return date.today()
    if isinstance(end_date, date):
        return end_date
    s = str(end_date).replace("-", "")
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def copy_history_from_production(
    engine: Engine,
    *,
    codes: List[str] | None = None,
    lookback_days: int = 15,
    end_date: str | date | None = None,
) -> int:
    """从 stock_daily 拷历史日线到 tmp 库（排除 today，today 由 1min 聚合填）。

    Args:
        engine: tmp 库 engine
        codes: 限定股票代码列表（None 表示全部）
        lookback_days: 拷最近 N 天
        end_date: 截止日期（默认今天）

    Returns:
        实际写入的行数
    """
    from src.storage import DatabaseManager

    end_d = _normalize_end_date(end_date)
    # 排除 today，因为 today 由 1min 聚合填，避免覆盖
    cutoff = end_d - timedelta(days=lookback_days + 5)
    end_cutoff = end_d - timedelta(days=1)  # 截止到昨天

    db_prod = DatabaseManager()
    with db_prod.get_session() as session:
        sql = (
            "SELECT code, date, open, high, low, close, volume, amount, pct_chg "
            "FROM stock_daily "
            "WHERE date >= :cutoff AND date <= :end_cutoff"
        )
        params: dict = {
            "cutoff": cutoff.strftime("%Y-%m-%d"),
            "end_cutoff": end_cutoff.strftime("%Y-%m-%d"),
        }
        if codes:
            placeholders = ",".join(f":c{i}" for i in range(len(codes)))
            sql += f" AND code IN ({placeholders})"
            for i, c in enumerate(codes):
                params[f"c{i}"] = c
        sql += " ORDER BY code, date"
        result = session.execute(text(sql), params)
        rows = result.fetchall()

    if not rows:
        logger.warning("[IntradayDB] stock_daily 无可拷历史数据")
        return 0

    # 写入 tmp 库
    df = pd.DataFrame(rows, columns=list(result.keys()))
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    n = _bulk_insert_daily(engine, df)
    logger.info(
        "[IntradayDB] stock_daily 拷贝完成: %d 行（%s ~ %s）",
        n, cutoff, end_cutoff,
    )
    return n


def _bulk_insert_daily(engine: Engine, df: pd.DataFrame) -> int:
    """批量插入日线数据到 tmp 库。"""
    if df.empty:
        return 0
    records = []
    for _, r in df.iterrows():
        records.append({
            "code": str(r["code"]),
            "date": str(r["date"]),
            "open": float(r["open"]) if pd.notna(r.get("open")) else None,
            "high": float(r["high"]) if pd.notna(r.get("high")) else None,
            "low": float(r["low"]) if pd.notna(r.get("low")) else None,
            "close": float(r["close"]) if pd.notna(r.get("close")) else None,
            "volume": float(r["volume"]) if pd.notna(r.get("volume")) else None,
            "amount": float(r["amount"]) if pd.notna(r.get("amount")) else None,
            "pct_chg": float(r["pct_chg"]) if pd.notna(r.get("pct_chg")) else None,
        })
    sql = text("""
        INSERT OR REPLACE INTO stock_daily_intraday_tmp
        (code, date, open, high, low, close, volume, amount, pct_chg)
        VALUES (:code, :date, :open, :high, :low, :close, :volume, :amount, :pct_chg)
    """)
    with engine.begin() as conn:
        conn.execute(sql, records)
    return len(records)
