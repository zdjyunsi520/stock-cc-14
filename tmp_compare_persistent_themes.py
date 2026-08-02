# -*- coding: utf-8 -*-
"""对比 concept_cache JSON vs stock_concept_membership 表
对 _find_persistent_themes 算法结果的影响。

不修改原代码，只模拟算法 + 对比两个数据源。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from sqlalchemy import text

from src.storage import DatabaseManager

logging.basicConfig(level=logging.WARNING)

DATE_KEY = "20260618"
LOOKBACK_DAYS = 15
TOP_N = 20
RECENT_DAYS = 5
DAILY_TOP = 5
MIN_DAYS = 3
PCT_THRESHOLD = 3.0


def load_universe_from_json(date_key: str) -> Dict[str, List[str]]:
    fp = Path("data/concept_cache") / f"concept_universe_{date_key}.json"
    if not fp.exists():
        return {}
    with open(fp, "r", encoding="utf-8") as f:
        return {str(k): list(v) for k, v in json.load(f).items()}


def load_universe_from_membership() -> Dict[str, List[str]]:
    db = DatabaseManager()
    universe: Dict[str, List[str]] = {}
    with db.get_session() as s:
        rows = s.execute(
            text("SELECT code, concept_name FROM stock_concept_membership")
        ).fetchall()
    for code, name in rows:
        if not code or not name:
            continue
        universe.setdefault(str(code), []).append(name)
    return universe


def find_persistent(
    metrics_df: pd.DataFrame,
    universe: Dict[str, List[str]],
) -> List[Tuple[str, int, List[str]]]:
    """模拟 PatternScreener._find_persistent_themes。

    Returns:
        [(concept_name, hit_days, [dates])]
    """
    trading_dates = sorted(metrics_df["date"].unique(), reverse=True)[:RECENT_DAYS]
    concept_day_count: Counter = Counter()
    concept_dates: Dict[str, List[str]] = {}

    for d in trading_dates:
        day_df = metrics_df[(metrics_df["date"] == d) & (metrics_df["pct_chg"] >= PCT_THRESHOLD)]
        day_concepts: Counter = Counter()
        for code in day_df["code"].unique():
            for t in universe.get(str(code), []):
                day_concepts[t] += 1
        for concept, _ in day_concepts.most_common(DAILY_TOP):
            concept_day_count[concept] += 1
            concept_dates.setdefault(concept, []).append(str(d))

    min_days = min(MIN_DAYS, len(trading_dates))
    persistent = [
        (c, n, sorted(concept_dates[c], reverse=True))
        for c, n in concept_day_count.most_common()
        if n >= min_days
    ]
    return persistent[:10]


def main() -> None:
    db = DatabaseManager()
    df = db.get_bulk_daily_data(days=LOOKBACK_DAYS, end_date=DATE_KEY)
    print(f"loaded stock_daily: {len(df)} rows, dates={sorted(df['date'].unique(), reverse=True)[:5]}")

    metrics_df = df[["code", "date", "pct_chg"]].copy()
    universe_json = load_universe_from_json(DATE_KEY)
    universe_db = load_universe_from_membership()
    print(f"\nuniverse_json: {len(universe_json)} codes (from {DATE_KEY}.json)")
    print(f"universe_db:   {len(universe_db)} codes (from stock_concept_membership)")

    print("\n--- 跑算法 ---")
    persistent_json = find_persistent(metrics_df, universe_json)
    persistent_db = find_persistent(metrics_df, universe_db)

    print("\n=== A. concept_cache JSON 持续热点 ===")
    if not persistent_json:
        print("(无)")
    for c, n, dates in persistent_json:
        print(f"  {c}  {n}天  {dates}")

    print("\n=== B. stock_concept_membership 持续热点 ===")
    if not persistent_db:
        print("(无)")
    for c, n, dates in persistent_db:
        print(f"  {c}  {n}天  {dates}")

    json_set = {c for c, _, _ in persistent_json}
    db_set = {c for c, _, _ in persistent_db}
    print(f"\n=== 差异 ===")
    print(f"A∩B 共有:  {sorted(json_set & db_set)}")
    print(f"仅 A 有:   {sorted(json_set - db_set)}")
    print(f"仅 B 有:   {sorted(db_set - json_set)}")


if __name__ == "__main__":
    main()
