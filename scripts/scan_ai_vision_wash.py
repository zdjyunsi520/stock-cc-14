# -*- coding: utf-8 -*-
"""扫描 AI 视觉相关概念的洗盘股。

题材映射：
  AI视觉 = 机器视觉 + 人脸识别 + AI眼镜 + AI视频
  （CIS/镜头等上游分散在芯片/光学里，已由集成电路+共封装光学覆盖，本次聚焦下游算法/应用）
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
    "AI视觉": ["机器视觉", "人脸识别", "AI眼镜", "AI视频"],
}

SCAN_DATE = "2026-06-22"


def load_theme_pool() -> dict[str, dict[str, list[str]]]:
    conn = sqlite3.connect(str(ROOT / "data" / "stock_analysis.db"))
    cur = conn.cursor()
    out = {}
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


def _disp_w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, w, a="left"):
    s = str(s)
    p = max(0, w - _disp_w(s))
    return " " * p + s if a == "right" else s + " " * p


def table(headers, rows, aligns):
    widths = []
    for i, h in enumerate(headers):
        w = _disp_w(h)
        for r in rows:
            if i < len(r) and r[i] is not None:
                w = max(w, _disp_w(r[i]))
        widths.append(w)
    b = lambda l, m, r: l + m.join("─" * (w + 2) for w in widths) + r
    fr = lambda vs: "│" + "│".join(" " + _pad(v if v is not None else "-", w, a) + " "
                                    for v, w, a in zip(vs, widths, aligns)) + "│"
    print(b("┌", "┬", "┐"))
    print(fr(headers))
    if rows:
        print(b("├", "┼", "┤"))
    for r in rows:
        print(fr(r))
    print(b("└", "┴", "┘"))


def main() -> int:
    theme_pool = load_theme_pool()
    db = DatabaseManager()
    df_all = db.get_bulk_daily_data(days=80, end_date=SCAN_DATE)
    df_all["date"] = pd.to_datetime(df_all["date"]).dt.strftime("%Y-%m-%d")
    df_all = df_all.sort_values(["code", "date"]).reset_index(drop=True)

    name_df = pd.read_csv(ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv",
                          dtype={"code": str})
    name_df["code"] = name_df["code"].str.zfill(6)
    name_map = dict(zip(name_df["code"], name_df["name"]))

    for theme, pool in theme_pool.items():
        print(f"=== {theme}（池 {len(pool)} 只）截至 {SCAN_DATE} ===")

        all_recs = []
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
            all_recs.append({
                "code": code,
                "name": name_map.get(code, ""),
                "concepts": concepts,
                **st,
            })

        total = len(all_recs)
        wash_recs = [r for r in all_recs if r["wash"] > 0]
        # 精筛横盘洗盘
        filtered = [r for r in all_recs
                    if r["wash"] >= 4 and abs(r["pct_chg"]) <= 5 and r["vol_ratio"] < 1.5]
        strong = [r for r in all_recs if r["wash"] >= 10]
        print(f"扫描结果: 总 {total} 只, 洗盘>0={len(wash_recs)}, 精筛横盘洗盘={len(filtered)}, 强洗盘(wash>=10)={len(strong)}")
        print()

        filtered.sort(key=lambda x: -x["wash"])

        headers = ["代码", "名称", "Wash", "T涨%", "量比", "MA10%", "ret5%", "题材", "洗盘特征"]
        aligns = ["left", "left", "right", "right", "right", "right", "right", "left", "left"]
        rows = []
        for r in filtered[:30]:
            rows.append([
                r["code"],
                str(r["name"])[:8],
                r["wash"],
                f"{r['pct_chg']:+.2f}",
                f"{r['vol_ratio']:.2f}",
                f"{r['dist_ma10']:+.2f}",
                f"{r['ret5']:+.2f}",
                ",".join(r["concepts"])[:12],
                r["wash_detail"][:26],
            ])
        if rows:
            table(headers, rows, aligns)
        else:
            print("  (无精筛命中)")

        out = ROOT / "data" / "_tmp_theme_filter" / "ai_vision_wash_scan.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({
                "scan_date": SCAN_DATE,
                "concept_map": CONCEPT_MAP,
                "pool_size": len(pool),
                "records": filtered,
            }, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
