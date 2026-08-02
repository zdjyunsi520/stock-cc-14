# -*- coding: utf-8 -*-
"""对比 5 天（06-15、06-16、06-17、06-18、06-22）推送股票的 T-1 vs T 状态，提取共性。

对每一天：
  1. 从 pattern_cache/pattern_YYYYMMDD.json 读 25 只候选
  2. 用 FundFlowScreener 重放资金流过滤，得到 买入+观察 名单
  3. 对每只股票计算 T-1 和 T 的状态指标
  4. 汇总到统一表

输出：
  - 5 天明细表（按日期分组）
  - 共性特征统计（涨幅/量比/distance/wash 的均值、中位数、分布）
  - 跨日稳定的特征（5 天都成立的规律）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import defaultdict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.fund_flow_screener import FundFlowScreener
from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

PUSH_DATES = ["20260615", "20260616", "20260617", "20260618", "20260622"]


def compute_state(g: pd.DataFrame) -> dict:
    if len(g) < 21:
        return {}
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    open_ = g["open"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close.iloc[-1])
    h = float(high.iloc[-1])
    l = float(low.iloc[-1])

    ma10 = float(close.rolling(10).mean().iloc[-1])
    avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
    vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0

    ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
    ret3 = float(sum(pct.iloc[-3:])) if len(pct) >= 3 else 0
    dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0
    close_pos = (cur - l) / (h - l) if (h - l) > 0 else 0.5
    pct_chg = float(pct.iloc[-1]) if len(pct) else 0

    wash, wash_detail = PatternScreener._compute_wash_score(g)

    return {
        "close": round(cur, 2),
        "pct_chg": round(pct_chg, 2),
        "vol_ratio": round(vol_ratio, 2),
        "dist_ma10": round(dist_ma10, 2),
        "ret5": round(ret5, 2),
        "ret3": round(ret3, 2),
        "close_pos": round(close_pos, 3),
        "wash": wash,
        "wash_detail": wash_detail,
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


def load_pushed(db, ffs, date_key: str):
    """从 pattern_cache 读候选，跑资金流过滤，返回买入+观察的 code/name 字典。"""
    cache_path = ROOT / "data" / "pattern_cache" / f"pattern_{date_key}.json"
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    candidates = cache["candidates"]

    class C:
        def __init__(self, code, name):
            self.code = code
            self.name = name

    cands = [C(c["code"], c["name"]) for c in candidates]
    buy, observe, avoid = ffs.filter_candidates(cands, date_key=date_key)
    pushed = {v.code: v.name for v in buy + observe}
    return pushed, len(buy), len(observe), len(avoid)


def main() -> int:
    db = DatabaseManager()
    ffs = FundFlowScreener(db=db)

    # 拉取数据（截至 06-22，向前 90 天）
    df_all = db.get_bulk_daily_data(days=90, end_date="2026-06-22")
    df_all["date"] = pd.to_datetime(df_all["date"]).dt.strftime("%Y-%m-%d")
    df_all = df_all.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"加载 {len(df_all)} 行, {df_all['code'].nunique()} 只股票")

    # 日期映射：YYYYMMDD -> YYYY-MM-DD
    date_map = {d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in PUSH_DATES}

    all_records = []
    summary_by_day = {}

    for date_key in PUSH_DATES:
        d_iso = date_map[date_key]
        pushed, n_buy, n_obs, n_avd = load_pushed(db, ffs, date_key)
        print(f"\n{d_iso}: 候选 25 → 买入 {n_buy} / 观察 {n_obs} / 回避 {n_avd}, 推送 {len(pushed)} 只")

        for code, name in pushed.items():
            g_full = df_all[df_all["code"] == code].reset_index(drop=True)
            if len(g_full) < 30:
                continue
            g_t = g_full[g_full["date"] <= d_iso].tail(60).reset_index(drop=True)
            g_prev = g_t.iloc[:-1].reset_index(drop=True) if len(g_t) > 1 else g_t
            if len(g_t) < 21 or len(g_prev) < 21:
                continue

            st_t = compute_state(g_t)
            st_p = compute_state(g_prev)
            if not st_t or not st_p:
                continue

            # 计算 score（规律分）
            def score(s):
                s1 = 10.0
                s2 = min(s["vol_ratio"], 3.0) * 10
                up5 = int(sum(1 for p in g_t["pct_chg"].astype(float).iloc[-5:] if p > 0))
                s3 = up5 * 5
                s4 = 10 if 0 <= s["dist_ma10"] <= 5 else 0
                s5 = -5 if s["ret5"] > 10 else 0
                return round(s1 + s2 + s3 + s4 + s5, 1)

            score_t = score(st_t)
            score_p = score(st_p)
            cat_t = categorize(score_t, st_t["wash"])
            cat_p = categorize(score_p, st_p["wash"])

            all_records.append({
                "date": d_iso,
                "code": code,
                "name": name,
                "prev_close": st_p["close"],
                "push_close": st_t["close"],
                "price_chg_1d": round((st_t["close"] / st_p["close"] - 1) * 100, 2),
                "push_pct": st_t["pct_chg"],
                "prev_pct": st_p["pct_chg"],
                "prev_vol_ratio": st_p["vol_ratio"],
                "push_vol_ratio": st_t["vol_ratio"],
                "vol_ratio_delta": round(st_t["vol_ratio"] - st_p["vol_ratio"], 2),
                "prev_dist_ma10": st_p["dist_ma10"],
                "push_dist_ma10": st_t["dist_ma10"],
                "dist_ma10_delta": round(st_t["dist_ma10"] - st_p["dist_ma10"], 2),
                "prev_close_pos": st_p["close_pos"],
                "push_close_pos": st_t["close_pos"],
                "prev_ret5": st_p["ret5"],
                "push_ret5": st_t["ret5"],
                "prev_score": score_p,
                "push_score": score_t,
                "score_delta": round(score_t - score_p, 1),
                "prev_wash": st_p["wash"],
                "push_wash": st_t["wash"],
                "wash_delta": st_t["wash"] - st_p["wash"],
                "prev_wash_detail": st_p["wash_detail"],
                "push_wash_detail": st_t["wash_detail"],
                "prev_cat": cat_p,
                "push_cat": cat_t,
            })

    # === 5 天汇总表 ===
    print("\n" + "=" * 230)
    print(f"{'日期':<12}{'代码':<7}{'名称':<10}{'前收':>7}{'今收':>7}{'1日涨':>7}"
          f"{'前量比':>7}{'今量比':>7}{'量比Δ':>7}"
          f"{'前MA10%':>9}{'今MA10%':>9}{'MA10Δ':>8}"
          f"{'前收位':>7}{'今收位':>7}"
          f"{'前规律':>7}{'今规律':>7}{'规律Δ':>7}"
          f"{'前洗盘':>6}{'今洗盘':>6}{'洗盘Δ':>6}")
    print("-" * 230)
    for r in all_records:
        print(f"{r['date']:<12}{r['code']:<7}{r['name'][:8]:<10}"
              f"{r['prev_close']:>7.2f}{r['push_close']:>7.2f}{r['price_chg_1d']:>+7.2f}"
              f"{r['prev_vol_ratio']:>7.2f}{r['push_vol_ratio']:>7.2f}{r['vol_ratio_delta']:>+7.2f}"
              f"{r['prev_dist_ma10']:>+9.2f}{r['push_dist_ma10']:>+9.2f}{r['dist_ma10_delta']:>+8.2f}"
              f"{r['prev_close_pos']:>7.2f}{r['push_close_pos']:>7.2f}"
              f"{r['prev_score']:>7.1f}{r['push_score']:>7.1f}{r['score_delta']:>+7.1f}"
              f"{r['prev_wash']:>6}{r['push_wash']:>6}{r['wash_delta']:>+6}")

    # === 按天统计共性 ===
    print("\n=== 按天统计 ===")
    print(f"{'日期':<12}{'样本':>5}{'均涨':>7}{'上涨率':>7}"
          f"{'均量比Δ':>8}{'放量率':>7}"
          f"{'前MA10中位':>11}{'今MA10中位':>11}{'MA10Δ中位':>10}"
          f"{'前收位中位':>10}{'今收位中位':>10}{'收位>0.8率':>10}"
          f"{'前洗盘中位':>10}{'今洗盘中位':>10}")
    print("-" * 150)
    for date_key in PUSH_DATES:
        d_iso = date_map[date_key]
        recs = [r for r in all_records if r["date"] == d_iso]
        if not recs:
            continue
        df_d = pd.DataFrame(recs)
        up_rate = (df_d["price_chg_1d"] > 0).sum() / len(df_d) * 100
        vol_up_rate = (df_d["vol_ratio_delta"] > 0).sum() / len(df_d) * 100
        close_high_rate = (df_d["push_close_pos"] >= 0.8).sum() / len(df_d) * 100
        print(f"{d_iso:<12}{len(df_d):>5}"
              f"{df_d['price_chg_1d'].mean():>+7.2f}{up_rate:>6.1f}%"
              f"{df_d['vol_ratio_delta'].mean():>+8.2f}{vol_up_rate:>6.1f}%"
              f"{df_d['prev_dist_ma10'].median():>+11.2f}{df_d['push_dist_ma10'].median():>+11.2f}"
              f"{df_d['dist_ma10_delta'].median():>+10.2f}"
              f"{df_d['prev_close_pos'].median():>10.2f}{df_d['push_close_pos'].median():>10.2f}{close_high_rate:>9.1f}%"
              f"{df_d['prev_wash'].median():>10.1f}{df_d['push_wash'].median():>10.1f}")

    # === 跨日共性特征 ===
    print("\n=== 跨日共性特征（5 天汇总，共 %d 只）===" % len(all_records))
    df_all_recs = pd.DataFrame(all_records)

    def stat(col):
        s = df_all_recs[col]
        return {
            "mean": round(s.mean(), 2),
            "median": round(s.median(), 2),
            "min": round(s.min(), 2),
            "max": round(s.max(), 2),
            "q25": round(s.quantile(0.25), 2),
            "q75": round(s.quantile(0.75), 2),
        }

    print(f"\n1) T 日涨幅（pct_chg）：{stat('push_pct')}")
    print(f"   上涨占比: {(df_all_recs['push_pct'] > 0).sum()}/{len(df_all_recs)} = "
          f"{(df_all_recs['push_pct'] > 0).sum() / len(df_all_recs) * 100:.1f}%")
    print(f"   涨 ≥ +3% 占比: {(df_all_recs['push_pct'] >= 3).sum()}/{len(df_all_recs)} = "
          f"{(df_all_recs['push_pct'] >= 3).sum() / len(df_all_recs) * 100:.1f}%")
    print(f"   涨 ≥ +5% 占比: {(df_all_recs['push_pct'] >= 5).sum()}/{len(df_all_recs)} = "
          f"{(df_all_recs['push_pct'] >= 5).sum() / len(df_all_recs) * 100:.1f}%")
    print(f"   涨停(≥+9.8%) 占比: {(df_all_recs['push_pct'] >= 9.8).sum()}/{len(df_all_recs)} = "
          f"{(df_all_recs['push_pct'] >= 9.8).sum() / len(df_all_recs) * 100:.1f}%")

    print(f"\n2) T 日量比（vol_ratio）：{stat('push_vol_ratio')}")
    print(f"   T-1 量比: {stat('prev_vol_ratio')}")
    print(f"   量比 Δ: {stat('vol_ratio_delta')}")
    print(f"   放量 (Δ>0.2) 占比: {(df_all_recs['vol_ratio_delta'] > 0.2).sum() / len(df_all_recs) * 100:.1f}%")

    print(f"\n3) T-1 dist_ma10：{stat('prev_dist_ma10')}")
    print(f"   T 日 dist_ma10：{stat('push_dist_ma10')}")
    print(f"   dist_ma10 Δ：{stat('dist_ma10_delta')}")
    print(f"   T-1 dist_ma10 ∈ [-5, +5] 占比: "
          f"{((df_all_recs['prev_dist_ma10'] >= -5) & (df_all_recs['prev_dist_ma10'] <= 5)).sum() / len(df_all_recs) * 100:.1f}%")
    print(f"   T 日 dist_ma10 > +5% 占比: "
          f"{(df_all_recs['push_dist_ma10'] > 5).sum() / len(df_all_recs) * 100:.1f}%")

    print(f"\n4) T 日 close_pos：{stat('push_close_pos')}")
    print(f"   T-1 close_pos：{stat('prev_close_pos')}")
    print(f"   T 日 close_pos ≥ 0.8 占比: "
          f"{(df_all_recs['push_close_pos'] >= 0.8).sum() / len(df_all_recs) * 100:.1f}%")

    print(f"\n5) T-1 洗盘分：{stat('prev_wash')}")
    print(f"   T 日洗盘分：{stat('push_wash')}")
    print(f"   T-1 wash > 0 占比: "
          f"{(df_all_recs['prev_wash'] > 0).sum() / len(df_all_recs) * 100:.1f}%")
    print(f"   T-1 wash ≥ 4 占比: "
          f"{(df_all_recs['prev_wash'] >= 4).sum() / len(df_all_recs) * 100:.1f}%")

    print(f"\n6) T-1 ret5：{stat('prev_ret5')}")
    print(f"   T-1 ret5 ≤ +15% 占比: "
          f"{(df_all_recs['prev_ret5'] <= 15).sum() / len(df_all_recs) * 100:.1f}%")

    # === 洗盘特征 tag 频次 ===
    print(f"\n7) T-1 洗盘特征 tag 频次：")
    tag_count_prev = defaultdict(int)
    tag_count_push = defaultdict(int)
    for r in all_records:
        for tag in r["prev_wash_detail"].split(","):
            tag = tag.strip()
            if tag:
                tag_count_prev[tag] += 1
        for tag in r["push_wash_detail"].split(","):
            tag = tag.strip()
            if tag:
                tag_count_push[tag] += 1
    total = len(all_records)
    print(f"{'特征':<16}{'T-1 占比':>10}{'T 日占比':>10}")
    for tag in sorted(set(list(tag_count_prev.keys()) + list(tag_count_push.keys()))):
        p_prev = tag_count_prev.get(tag, 0) / total * 100
        p_push = tag_count_push.get(tag, 0) / total * 100
        print(f"{tag:<16}{p_prev:>9.1f}%{p_push:>9.1f}%")

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "push_5day_analysis.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "records": all_records,
            "tag_count_prev": dict(tag_count_prev),
            "tag_count_push": dict(tag_count_push),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
