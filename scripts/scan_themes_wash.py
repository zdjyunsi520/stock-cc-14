# -*- coding: utf-8 -*-
"""扫描指定题材池里的洗盘股。

题材映射（基于同花顺概念库，由用户口径转译）：
  集成电路 = 芯片概念 + 存储芯片 + 第三代半导体 + 汽车芯片 + MCU芯片 + 光刻机 + 光刻胶 + 中芯国际概念
  工业母机 = 工业母机
  基础软件 = 国产操作系统 + 信创
  高端仪器 = 同花顺无对应概念（库里只有医疗类「血氧仪」），本脚本不覆盖

输出：
  - 按题材分组的洗盘股明细（wash_score > 0）
  - 洗盘特征 tag 频次
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

CONCEPT_MAP = {
    "集成电路": ["芯片概念", "存储芯片", "第三代半导体", "汽车芯片", "MCU芯片",
                "光刻机", "光刻胶", "中芯国际概念"],
    "工业母机": ["工业母机"],
    "基础软件": ["国产操作系统", "信创"],
}

SCAN_DATE = "2026-06-22"


def load_theme_pool() -> dict[str, dict[str, list[str]]]:
    """返回 {theme: {code: [concept1, concept2, ...]}}。"""
    conn = sqlite3.connect(str(ROOT / "data" / "stock_analysis.db"))
    cur = conn.cursor()
    out: dict[str, dict[str, list[str]]] = {}
    for theme, concepts in CONCEPT_MAP.items():
        placeholders = ",".join(["?"] * len(concepts))
        rows = cur.execute(
            f"SELECT code, concept_name FROM stock_concept_membership "
            f"WHERE concept_name IN ({placeholders})",
            concepts,
        ).fetchall()
        pool: dict[str, list[str]] = defaultdict(list)
        for code, cn in rows:
            pool[code].append(cn)
        out[theme] = dict(pool)
    conn.close()
    return out


def compute_state(g: pd.DataFrame) -> dict:
    if len(g) < 21:
        return {}
    close = g["close"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close.iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
    vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0
    ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
    dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0
    wash, detail = PatternScreener._compute_wash_score(g)
    return {
        "close": round(cur, 2),
        "pct_chg": round(float(pct.iloc[-1]) if len(pct) else 0, 2),
        "vol_ratio": round(vol_ratio, 2),
        "dist_ma10": round(dist_ma10, 2),
        "ret5": round(ret5, 2),
        "wash": wash,
        "wash_detail": detail,
    }


def _disp_w(s) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width: int, align: str = "left") -> str:
    s = str(s)
    pad = max(0, width - _disp_w(s))
    if align == "right":
        return " " * pad + s
    return s + " " * pad


def print_table(headers: list[str], rows: list[list], aligns: list[str]) -> None:
    widths = []
    for i, h in enumerate(headers):
        w = _disp_w(h)
        for row in rows:
            if i < len(row) and row[i] is not None:
                w = max(w, _disp_w(row[i]))
        widths.append(w)

    def border(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r

    def fmt_row(values):
        cells = [" " + _pad(v if v is not None else "-", w, a) + " "
                 for v, w, a in zip(values, widths, aligns)]
        return "│" + "│".join(cells) + "│"

    print(border("┌", "┬", "┐"))
    print(fmt_row(headers))
    if rows:
        print(border("├", "┼", "┤"))
    for row in rows:
        print(fmt_row(row))
    print(border("└", "┴", "┘"))


def main() -> int:
    theme_pool = load_theme_pool()
    db = DatabaseManager()
    df_all = db.get_bulk_daily_data(days=80, end_date=SCAN_DATE)
    df_all["date"] = pd.to_datetime(df_all["date"]).dt.strftime("%Y-%m-%d")
    df_all = df_all.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"加载 {len(df_all)} 行, {df_all['code'].nunique()} 只股票, 截至 {SCAN_DATE}")
    print()

    out_records: dict[str, list[dict]] = {}
    tag_freq: dict[str, dict[str, int]] = {}

    for theme, pool in theme_pool.items():
        print(f"=== {theme}（{len(pool)} 只）===")
        wash_hits = []
        tag_count: dict[str, int] = defaultdict(int)
        for code, concepts in pool.items():
            g_full = df_all[df_all["code"] == code].reset_index(drop=True)
            if len(g_full) < 30:
                continue
            g = g_full[g_full["date"] <= SCAN_DATE].tail(60).reset_index(drop=True)
            if len(g) < 21:
                continue
            st = compute_state(g)
            if not st:
                continue
            if st["wash"] > 0:
                wash_hits.append({
                    "code": code,
                    "concepts": concepts,
                    **st,
                })
                for tag in st["wash_detail"].split(","):
                    tag = tag.strip()
                    if tag:
                        # 去掉量化后缀（如 "贴MA10" 不带数字，"下影1x" 带数字）
                        tag_count[tag] += 1

        wash_hits.sort(key=lambda x: -x["wash"])
        out_records[theme] = wash_hits
        tag_freq[theme] = dict(tag_count)
        print(f"洗盘股: {len(wash_hits)} / {len(pool)} 只")

        # 表格输出
        headers = ["代码", "名称", "Wash", "T涨%", "量比", "MA10%", "ret5%", "洗盘特征"]
        aligns = ["left", "left", "right", "right", "right", "right", "right", "left"]

        # 拿股票名
        name_csv = ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv"
        name_df = pd.read_csv(name_csv, dtype={"code": str})
        name_df["code"] = name_df["code"].str.zfill(6)
        name_map = dict(zip(name_df["code"], name_df["name"]))

        rows = []
        for r in wash_hits:
            rows.append([
                r["code"],
                str(name_map.get(r["code"], ""))[:8],
                r["wash"],
                f"{r['pct_chg']:+.2f}",
                f"{r['vol_ratio']:.2f}",
                f"{r['dist_ma10']:+.2f}",
                f"{r['ret5']:+.2f}",
                r["wash_detail"][:24],
            ])
        if rows:
            print_table(headers, rows, aligns)
        else:
            print("  (无洗盘股)")
        print()

    # tag 频次汇总
    print("=== 各题材洗盘特征 tag 频次 TOP ===")
    for theme, tf in tag_freq.items():
        if not tf:
            continue
        print(f"\n[{theme}]")
        for tag, cnt in sorted(tf.items(), key=lambda x: -x[1])[:10]:
            print(f"  {tag:<16} {cnt} 只")

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "themes_wash_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "scan_date": SCAN_DATE,
            "concept_map": CONCEPT_MAP,
            "records": out_records,
            "tag_freq": tag_freq,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
