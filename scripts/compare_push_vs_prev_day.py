# -*- coding: utf-8 -*-
"""对比 06-22 推送的 25 只候选股，在 06-19（前一交易日）vs 06-22（推送日）的状态差异。

对每只股票计算：
  - 06-19 和 06-22 的 close、pct_chg、volume、vol_ratio(对比 ma5)
  - 06-19 和 06-22 的 dist_ma10、score、wash_score
  - 状态变化：新出现 / 已在榜单 / 掉出榜单
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager


def compute_state(g: pd.DataFrame) -> dict:
    """计算一只股票在某个时间点的状态指标（用 g 的最后一条作为 T 日）。"""
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
        "volume": round(float(volume.iloc[-1]) / 1e6, 2),  # 万手 -> 简化为百万股
        "vol_ratio": round(vol_ratio, 2),
        "dist_ma10": round(dist_ma10, 2),
        "ret5": round(ret5, 2),
        "up_days_5": up5,
        "score": round(score, 1),
        "wash": wash,
        "wash_detail": detail,
    }


def categorize(score: float, wash: int) -> str:
    if wash == 0:
        return "excluded"
    if score <= 50 and wash >= 10:
        return "absorb"
    if score > 60 and wash > 0:
        return "strong_wash"
    if 50 < score <= 60 and wash >= 10:
        return "over_hot"
    return "excluded"


def main() -> int:
    cache_path = ROOT / "data" / "pattern_cache" / "pattern_20260622.json"
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    pushed = cache["candidates"]
    print(f"06-22 推送的候选股: {len(pushed)} 只")
    print()

    codes = [c["code"] for c in pushed]
    db = DatabaseManager()
    df_all = db.get_bulk_daily_data(days=80, end_date="2026-06-22")
    df = df_all[df_all["code"].isin(codes)].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    # 对比两个时点
    D_PUSH = "2026-06-22"   # 推送日
    D_PREV = "2026-06-19"   # 前一交易日（周五）

    rows = []
    for cand in pushed:
        code = cand["code"]
        name = cand["name"]
        g_full = df[df["code"] == code].reset_index(drop=True)
        if len(g_full) < 25:
            continue

        g_push = g_full[g_full["date"] <= D_PUSH].tail(60).reset_index(drop=True)
        g_prev = g_full[g_full["date"] <= D_PREV].tail(60).reset_index(drop=True)

        st_push = compute_state(g_push)
        st_prev = compute_state(g_prev)

        if not st_push or not st_prev:
            continue

        cat_push = categorize(st_push["score"], st_push["wash"])
        cat_prev = categorize(st_prev["score"], st_prev["wash"])

        rows.append({
            "code": code,
            "name": name,
            "themes": cand["themes"],
            "push_close": st_push["close"],
            "prev_close": st_prev["close"],
            "price_chg_1d": round((st_push["close"] / st_prev["close"] - 1) * 100, 2),
            "push_pct": st_push["pct_chg"],
            "push_vol_ratio": st_push["vol_ratio"],
            "prev_vol_ratio": st_prev["vol_ratio"],
            "vol_ratio_delta": round(st_push["vol_ratio"] - st_prev["vol_ratio"], 2),
            "push_dist_ma10": st_push["dist_ma10"],
            "prev_dist_ma10": st_prev["dist_ma10"],
            "dist_ma10_delta": round(st_push["dist_ma10"] - st_prev["dist_ma10"], 2),
            "push_score": st_push["score"],
            "prev_score": st_prev["score"],
            "score_delta": round(st_push["score"] - st_prev["score"], 1),
            "push_wash": st_push["wash"],
            "prev_wash": st_prev["wash"],
            "wash_delta": st_push["wash"] - st_prev["wash"],
            "push_wash_detail": st_push["wash_detail"],
            "prev_wash_detail": st_prev["wash_detail"],
            "push_cat": cat_push,
            "prev_cat": cat_prev,
        })

    # 推送日排序按 score 降序
    rows.sort(key=lambda x: -x["push_score"])

    # 输出大表
    print("=" * 200)
    print(f"{'代码':<7}{'名称':<10}{'题材数':<6}{'前收':>7}{'今收':>7}{'日涨':>7}"
          f"{'前量比':>7}{'今量比':>7}{'量比Δ':>7}"
          f"{'前MA10%':>9}{'今MA10%':>9}{'MA10Δ':>8}"
          f"{'前规律':>7}{'今规律':>7}{'规律Δ':>7}"
          f"{'前洗盘':>6}{'今洗盘':>6}{'洗盘Δ':>6}{'前状态':<10}{'今状态':<10}")
    print("-" * 200)
    cat_cn = {"absorb": "吸筹", "strong_wash": "强势洗盘", "over_hot": "过热", "excluded": "排除"}
    for r in rows:
        print(f"{r['code']:<7}{r['name'][:8]:<10}{len(r['themes']):<6}"
              f"{r['prev_close']:>7.2f}{r['push_close']:>7.2f}{r['price_chg_1d']:>+7.2f}"
              f"{r['prev_vol_ratio']:>7.2f}{r['push_vol_ratio']:>7.2f}{r['vol_ratio_delta']:>+7.2f}"
              f"{r['prev_dist_ma10']:>+9.2f}{r['push_dist_ma10']:>+9.2f}{r['dist_ma10_delta']:>+8.2f}"
              f"{r['prev_score']:>7.1f}{r['push_score']:>7.1f}{r['score_delta']:>+7.1f}"
              f"{r['prev_wash']:>6}{r['push_wash']:>6}{r['wash_delta']:>+6}"
              f"{(cat_cn.get(r['prev_cat'], r['prev_cat'])):<10}{(cat_cn.get(r['push_cat'], r['push_cat'])):<10}")

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "push_vs_prev_day.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")

    # 汇总
    print("\n=== 汇总 ===")
    newly_appeared = [r for r in rows if r["prev_cat"] == "excluded" and r["push_cat"] != "excluded"]
    score_up = [r for r in rows if r["score_delta"] > 0]
    score_down = [r for r in rows if r["score_delta"] < 0]
    wash_up = [r for r in rows if r["wash_delta"] > 0]
    vol_expand = [r for r in rows if r["vol_ratio_delta"] > 0.2]
    vol_shrink = [r for r in rows if r["vol_ratio_delta"] < -0.2]

    print(f"前一日非候选 → 今日新进: {len(newly_appeared)} 只")
    for r in newly_appeared:
        print(f"  {r['code']} {r['name']}: 规律 {r['prev_score']}→{r['push_score']}({r['score_delta']:+}), "
              f"洗盘 {r['prev_wash']}→{r['push_wash']}({r['wash_delta']:+}), 量比 {r['prev_vol_ratio']}→{r['push_vol_ratio']}")

    print(f"\n规律分上涨: {len(score_up)} 只, 下降: {len(score_down)} 只")
    print(f"洗盘分上涨: {len(wash_up)} 只")
    print(f"量比显著放大(>0.2): {len(vol_expand)} 只, 显著缩量(<-0.2): {len(vol_shrink)} 只")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
