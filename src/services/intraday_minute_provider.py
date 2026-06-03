# -*- coding: utf-8 -*-
"""Cached intraday minute-line provider for stock screening."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from data_provider.base import normalize_stock_code
from src.config import get_config

logger = logging.getLogger(__name__)


class IntradayMinuteCacheProvider:
    """Fetch and cache one-minute bars for a single A-share symbol."""

    def __init__(self, *, db_path: Optional[str] = None, ttl_seconds: int = 60) -> None:
        config = get_config()
        self.db_path = Path(db_path or config.database_path)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._ensure_table()

    def __call__(self, code: str) -> List[Dict[str, Any]]:
        return self.get_minutes(code)

    def get_minutes(self, code: str) -> List[Dict[str, Any]]:
        code = normalize_stock_code(str(code or ""))
        if not code:
            return []

        cached = self._load_cached(code)
        if cached and self._is_cache_fresh(code):
            return cached

        try:
            rows = self._fetch_from_akshare(code)
        except Exception as exc:
            logger.warning("[IntradayMinute] 获取 %s 分钟线失败: %s", code, exc)
            return cached

        if rows:
            self._save_rows(code, rows)
            return rows
        return cached

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_minute (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(10) NOT NULL,
                    trade_date DATE NOT NULL,
                    minute_time DATETIME NOT NULL,
                    open FLOAT,
                    high FLOAT,
                    low FLOAT,
                    close FLOAT,
                    volume FLOAT,
                    amount FLOAT,
                    avg_price FLOAT,
                    data_source VARCHAR(50),
                    fetched_at DATETIME NOT NULL,
                    UNIQUE (code, trade_date, minute_time)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_stock_minute_code_time ON stock_minute (code, minute_time)")
            conn.commit()

    def _is_cache_fresh(self, code: str) -> bool:
        if self.ttl_seconds <= 0:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(fetched_at) AS fetched_at FROM stock_minute WHERE code = ?",
                (code,),
            ).fetchone()
        if row is None or not row["fetched_at"]:
            return False
        try:
            fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
        except ValueError:
            return False
        return time.time() - fetched_at.timestamp() < self.ttl_seconds

    def _load_cached(self, code: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            latest = conn.execute(
                "SELECT MAX(trade_date) AS trade_date FROM stock_minute WHERE code = ?",
                (code,),
            ).fetchone()
            trade_date = latest["trade_date"] if latest is not None else None
            if not trade_date:
                return []
            rows = conn.execute(
                """
                SELECT minute_time, open, high, low, close, volume, amount, avg_price, data_source
                FROM stock_minute
                WHERE code = ? AND trade_date = ?
                ORDER BY minute_time
                """,
                (code, trade_date),
            ).fetchall()
        return [
            {
                "time": row["minute_time"],
                "price": row["close"],
                "close": row["close"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "volume": row["volume"],
                "amount": row["amount"],
                "avg_price": row["avg_price"],
                "source": row["data_source"],
            }
            for row in rows
        ]

    def _fetch_from_akshare(self, code: str) -> List[Dict[str, Any]]:
        import akshare as ak

        symbol = ("sh" if code.startswith("6") else "sz") + code
        df = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        if df is None or df.empty:
            return []
        return self._normalize_minute_frame(df)

    def _normalize_minute_frame(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        data = df.copy()
        time_col = _first_existing_column(data, ("day", "时间", "datetime", "time"))
        close_col = _first_existing_column(data, ("close", "收盘", "price", "最新价"))
        if time_col is None or close_col is None:
            return []

        data["_minute_time"] = pd.to_datetime(data[time_col], errors="coerce")
        data = data.dropna(subset=["_minute_time"])
        if data.empty:
            return []
        latest_date = data["_minute_time"].dt.date.max()
        data = data[data["_minute_time"].dt.date == latest_date].sort_values("_minute_time")

        open_col = _first_existing_column(data, ("open", "开盘"))
        high_col = _first_existing_column(data, ("high", "最高"))
        low_col = _first_existing_column(data, ("low", "最低"))
        volume_col = _first_existing_column(data, ("volume", "成交量"))
        amount_col = _first_existing_column(data, ("amount", "成交额"))

        close = pd.to_numeric(data[close_col], errors="coerce")
        volume = pd.to_numeric(data[volume_col], errors="coerce") if volume_col else pd.Series([None] * len(data), index=data.index)
        amount = pd.to_numeric(data[amount_col], errors="coerce") if amount_col else pd.Series([None] * len(data), index=data.index)
        cum_volume = volume.fillna(0).cumsum()
        cum_amount = amount.fillna(0).cumsum()
        avg_price = (cum_amount / cum_volume).where(cum_volume > 0, close)

        rows: List[Dict[str, Any]] = []
        for idx, item in data.iterrows():
            minute_time = item["_minute_time"].to_pydatetime()
            rows.append(
                {
                    "trade_date": minute_time.date().isoformat(),
                    "time": minute_time.isoformat(sep=" "),
                    "open": _safe_float(item.get(open_col)) if open_col else None,
                    "high": _safe_float(item.get(high_col)) if high_col else None,
                    "low": _safe_float(item.get(low_col)) if low_col else None,
                    "close": _safe_float(item.get(close_col)),
                    "price": _safe_float(item.get(close_col)),
                    "volume": _safe_float(item.get(volume_col)) if volume_col else None,
                    "amount": _safe_float(item.get(amount_col)) if amount_col else None,
                    "avg_price": _safe_float(avg_price.loc[idx]),
                    "source": "akshare_stock_zh_a_minute",
                }
            )
        return [row for row in rows if row["close"] is not None and row["avg_price"] is not None]

    def _save_rows(self, code: str, rows: Sequence[Dict[str, Any]]) -> None:
        fetched_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        payload = [
            (
                code,
                row.get("trade_date") or str(row.get("time", ""))[:10],
                row.get("time"),
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close") or row.get("price"),
                row.get("volume"),
                row.get("amount"),
                row.get("avg_price"),
                row.get("source") or "unknown",
                fetched_at,
            )
            for row in rows
            if row.get("time")
        ]
        if not payload:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO stock_minute (
                    code, trade_date, minute_time, open, high, low, close,
                    volume, amount, avg_price, data_source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date, minute_time) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    avg_price = excluded.avg_price,
                    data_source = excluded.data_source,
                    fetched_at = excluded.fetched_at
                """,
                payload,
            )
            conn.commit()


def _first_existing_column(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
