# -*- coding: utf-8 -*-
"""重放 06-22 推送：对资金流过滤后的 11 只逐只判定，并对比 06-19 状态。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.fund_flow_screener import FundFlowScreener
from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

PUSH_11 = [
    ("002237", "恒邦股份"),
    ("603799", "华友钴业"),
    ("603077", "和邦生物"),
    ("002738", "中矿资源"),
    ("000758", "中色股份"),
    ("000060", "中金岭南"),
    ("601168", "西部矿业"),
    ("000546", "金圆股份"),
    ("002340", "格林美"),
    ("002455", "百川股份"),
    ("600111", "北方稀土"),
]


def compute_state(g: pd.DataFrame) -> dict:
    if len(g) < 10:
        return {}
    close = g["close"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close.iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
    vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0
    up5 = int(sum(1 for p in pct.iloc[-5:] if p > 0)) if len(pct) >= 5 else 0
    ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
    dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0
    s1 = 10.0
    s2 = min(vol_ratio, 3.0) * 10
    s3 = up5 * 5
    s4 = 10 if 0 <= dist_ma10 <= 5 else 0
    s5 = -5 if ret5 > 10 else 0
    score = s1 + s2 + s3 + s4 + s5
    wash, detail = (0, "")
    if len(g) >= 20:
        wash, detail = PatternScreener._compute_wash_score(g)
    return {
        "close": round(cur, 2),
        "pct_chg": round(float(pct.iloc[-1]) if len(pct) else 0, 2),
        "vol_ratio": round(vol_ratio, 2),
        "dist_ma10": round(dist_ma10, 2),
        "ret5": round(ret5, 2),
        "score": round(score, 1),
        "wash": wash,
        "wash_detail": detail,
    }


def main() -> int:
    db = DatabaseManager()

    # 重放资金流过滤
    ffs = FundFlowScreener(db=db)
    # 用个简单 namespace 当 candidate
    class C:
        def __init__(self, code, name):
            self.code = code
            self.name = name

    cands = [C(c, n) for c, n in PUSH_11]
    buy, observe, avoid = ffs.filter_candidates(cands, date_key="20260622")
    print(f"买入 {len(buy)} / 观察 {len(observe)} / 回避 {len(avoid)}")
    print()
    print("买入:", [(v.code, v.name) for v in buy])
    print("观察:", [(v.code, v.name) for v in observe])
    print("回避:", [(v.code, v.name) for v in avoid])
    print()

    pushed = [v.code for v in buy] + [v.code for v in observe]
    pushed_names = {v.code: v.name for v in buy + observe}

    # 对比 06-19 vs 06-22
    df_all = db.get_bulk_daily_data(days=80, end_date="2026-06-22")
    df = df_all[df_all["code"].isin(pushed)].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    rows = []
    for code in pushed:
        g_full = df[df["code"] == code].reset_index(drop=True)
        g_push = g_full[g_full["date"] <= "2026-06-22"].tail(60).reset_index(drop=True)
        g_prev = g_full[g_full["date"] <= "2026-06-19"].tail(60).reset_index(drop=True)
        st_p = compute_state(g_push)
        st_v = compute_state(g_prev)
        if not st_p or not st_v:
            continue
        rows.append({
            "code": code,
            "name": pushed_names.get(code, ""),
            "grade": "买入" if code in [v.code for v in buy] else "观察",
            "prev_close": st_v["close"],
            "push_close": st_p["close"],
            "price_chg_1d": round((st_p["close"] / st_v["close"] - 1) * 100, 2),
            "push_pct": st_p["pct_chg"],
            "prev_vol_ratio": st_v["vol_ratio"],
            "push_vol_ratio": st_p["vol_ratio"],
            "vol_ratio_delta": round(st_p["vol_ratio"] - st_v["vol_ratio"], 2),
            "prev_dist_ma10": st_v["dist_ma10"],
            "push_dist_ma10": st_p["dist_ma10"],
            "dist_ma10_delta": round(st_p["dist_ma10"] - st_v["dist_ma10"], 2),
            "prev_score": st_v["score"],
            "push_score": st_p["score"],
            "score_delta": round(st_p["score"] - st_v["score"], 1),
            "prev_wash": st_v["wash"],
            "push_wash": st_p["wash"],
            "wash_delta": st_p["wash"] - st_v["wash"],
            "prev_wash_detail": st_v["wash_detail"],
            "push_wash_detail": st_p["wash_detail"],
        })

    rows.sort(key=lambda x: -x["push_score"])

    print("=" * 170)
    print(f"{'级别':<5}{'代码':<7}{'名称':<10}{'前收':>7}{'今收':>7}{'1日涨':>7}"
          f"{'前量比':>7}{'今量比':>7}{'量比Δ':>7}"
          f"{'前MA10%':>9}{'今MA10%':>9}{'MA10Δ':>8}"
          f"{'前规律':>7}{'今规律':>7}{'规律Δ':>7}"
          f"{'前洗盘':>6}{'今洗盘':>6}{'洗盘Δ':>6}")
    print("-" * 170)
    for r in rows:
        print(f"{r['grade']:<5}{r['code']:<7}{r['name'][:8]:<10}"
              f"{r['prev_close']:>7.2f}{r['push_close']:>7.2f}{r['price_chg_1d']:>+7.2f}"
              f"{r['prev_vol_ratio']:>7.2f}{r['push_vol_ratio']:>7.2f}{r['vol_ratio_delta']:>+7.2f}"
              f"{r['prev_dist_ma10']:>+9.2f}{r['push_dist_ma10']:>+9.2f}{r['dist_ma10_delta']:>+8.2f}"
              f"{r['prev_score']:>7.1f}{r['push_score']:>7.1f}{r['score_delta']:>+7.1f}"
              f"{r['prev_wash']:>6}{r['push_wash']:>6}{r['wash_delta']:>+6}")

    print()
    print("=== 洗盘特征变化（前→今）===")
    for r in rows:
        if r["prev_wash_detail"] != r["push_wash_detail"]:
            print(f"  {r['code']} {r['name']}: [{r['prev_wash_detail']}] → [{r['push_wash_detail']}]")

    out = ROOT / "data" / "_tmp_theme_filter" / "push_10_vs_prev_day.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"buy": [v.code for v in buy],
                   "observe": [v.code for v in observe],
                   "avoid": [{"code": v.code, "name": v.name} for v in avoid],
                   "details": rows}, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
