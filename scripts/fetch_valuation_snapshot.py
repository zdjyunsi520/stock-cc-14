# -*- coding: utf-8 -*-
"""拉取最近交易日的全市场 PE/PB/PS 估值快照（tushare daily_basic）。

tushare 接口频率限制 1 次/分钟，故单次调用 trade_date 参数拉全市场，落地到 sqlite。

用法：
  python scripts/fetch_valuation_snapshot.py [--date YYYYMMDD]

输出：写入 stock_analysis.db 的 stock_valuation_daily 表（不存在则建）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import tushare as ts

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_config


def fetch_valuation(date_str: str) -> pd.DataFrame:
    """tushare daily_basic：单日全市场估值数据。"""
    cfg = get_config()
    ts.set_token(cfg.tushare_token)
    pro = ts.pro_api()

    print(f"[tushare] 拉取 {date_str} 全市场 daily_basic ...")
    t0 = time.time()
    df = pro.daily_basic(
        trade_date=date_str,
        fields="ts_code,trade_date,pe,pb,ps,total_mv,circ_mv,turnover_rate",
    )
    print(f"  返回 {len(df)} 行, 耗时 {time.time()-t0:.1f}s")
    return df


def save_to_db(df: pd.DataFrame, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_valuation_daily (
            code VARCHAR(10),
            trade_date DATE,
            pe FLOAT,
            pb FLOAT,
            ps FLOAT,
            total_mv FLOAT,
            circ_mv FLOAT,
            turnover_rate FLOAT,
            PRIMARY KEY (code, trade_date)
        )
    """)
    df["code"] = df["ts_code"].str[:6]
    df = df[["code", "trade_date", "pe", "pb", "ps", "total_mv", "circ_mv", "turnover_rate"]]
    df.to_sql("stock_valuation_daily", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()
    print(f"写入 {len(df)} 行 -> stock_valuation_daily")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="20260622",
                        help="YYYYMMDD 格式交易日（默认 20260622）")
    args = parser.parse_args()

    db_path = ROOT / "data" / "stock_analysis.db"
    df = fetch_valuation(args.date)
    if df.empty:
        print(f"[warn] {args.date} 无数据，可能非交易日")
        return 1
    save_to_db(df, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
