# -*- coding: utf-8 -*-
"""三题材低位吸筹选股。

在原有规律+洗盘评分基础上，增加"未大涨"过滤：
  1. 近 20 日累计涨幅 <= 12%（未明显上涨）
  2. 距 20 日高点回撤 <= -5%（不在高点）
  3. 现价距 MA20 <= +8%（不偏离均线）
  4. 近 20 日最低点到现在的反弹 <= 15%（不在反弹高位）
  5. 近 20 日内涨幅 < 20%（排除脉冲式拉升）

同时在 20 个交易日内逐日评分，统计出现低位吸筹的天数。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

WINDOW_DATES = [
    "2026-05-22", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28",
    "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
    "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11",
    "2026-06-12", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
]

# 低位过滤阈值 - 中等严格度（兼顾样本量）
MAX_RET_20D = 15.0      # 近 20 日累计涨幅上限 %
MIN_DRAWDOWN_FROM_HIGH = -5.0  # 距 20 日高点的最小回撤 %（负数）
MAX_DIST_MA20 = 10.0    # 现价距 MA20 上限 %
MAX_BOUNCE_FROM_LOW = 15.0  # 距 20 日低点的反弹上限 %
MAX_SINGLE_DAY_GAIN = 8.0   # 20 日内单日最大涨幅上限 %（排除涨停/脉冲）
MAX_AMPLITUDE_RATIO = 1.30  # 20 日最高/最低比上限（振幅 ≤30%）


def is_low_position(g: pd.DataFrame) -> tuple[bool, dict]:
    """检查最近一日是否满足"低位未大涨"条件。"""
    if len(g) < 21:
        return False, {}

    close = g["close"].astype(float)
    pct = g["pct_chg"].astype(float) if "pct_chg" in g.columns else pd.Series(dtype=float)
    cur = float(close.iloc[-1])

    closes_20 = close.tail(20)
    pcts_20 = pct.tail(20) if len(pct) >= 20 else pct
    high_20 = float(closes_20.max())
    low_20 = float(closes_20.min())
    close_20_ago = float(close.iloc[-20])

    ret_20 = (cur / close_20_ago - 1) * 100
    dd_high = (cur / high_20 - 1) * 100
    bounce_low = (cur / low_20 - 1) * 100
    amplitude_ratio = high_20 / low_20 if low_20 > 0 else 99
    max_single_gain = float(pcts_20.max()) if len(pcts_20) else 0
    limit_up_count_20d = int((pcts_20 >= 9.8).sum()) if len(pcts_20) else 0

    ma20 = float(close.rolling(20).mean().iloc[-1])
    dist_ma20 = (cur / ma20 - 1) * 100 if ma20 > 0 else 0

    metrics = {
        "ret_20d": round(ret_20, 2),
        "dd_high_20d": round(dd_high, 2),
        "bounce_low_20d": round(bounce_low, 2),
        "dist_ma20": round(dist_ma20, 2),
        "high_20d": round(high_20, 2),
        "low_20d": round(low_20, 2),
        "amplitude_ratio": round(amplitude_ratio, 3),
        "max_single_gain_20d": round(max_single_gain, 2),
        "limit_up_count_20d": limit_up_count_20d,
    }

    ok = (
        ret_20 <= MAX_RET_20D
        and dd_high <= MIN_DRAWDOWN_FROM_HIGH
        and dist_ma20 <= MAX_DIST_MA20
        and bounce_low <= MAX_BOUNCE_FROM_LOW
        and max_single_gain <= MAX_SINGLE_DAY_GAIN
        and amplitude_ratio <= MAX_AMPLITUDE_RATIO
        and limit_up_count_20d == 0
    )
    return ok, metrics


def score_one(g: pd.DataFrame) -> tuple[float, int, str]:
    """规律分 + 洗盘分（s1 热点题材分固定 10）。"""
    if len(g) < 10:
        return 0.0, 0, ""

    close_s = g["close"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close_s.iloc[-1])

    ma10 = float(close_s.rolling(10).mean().iloc[-1])
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

    return round(score, 1), wash, detail


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
    pool_path = ROOT / "data" / "_tmp_theme_filter" / "merged_pool.json"
    with open(pool_path, "r", encoding="utf-8") as f:
        pool = json.load(f)

    print(f"候选池: {len(pool)} 只")
    print(f"时间窗口: {WINDOW_DATES[0]} ~ {WINDOW_DATES[-1]}")
    print(f"低位过滤: 20日涨幅<={MAX_RET_20D}%, 距20日高点<={MIN_DRAWDOWN_FROM_HIGH}%, "
          f"距MA20<={MAX_DIST_MA20}%, 距20日低点反弹<={MAX_BOUNCE_FROM_LOW}%")

    db = DatabaseManager()
    df_all = db.get_bulk_daily_data(days=80, end_date=WINDOW_DATES[-1])
    print(f"加载 {len(df_all)} 行日线")

    df = df_all[df_all["code"].isin(pool.keys())].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"过滤后: {len(df)} 行, {df['code'].nunique()} 只")

    results = []
    t0 = time.time()
    for i, code in enumerate(sorted(pool.keys())):
        if (i + 1) % 500 == 0:
            print(f"  进度 {i+1}/{len(pool)}, 已用 {time.time()-t0:.0f}s")
        g_full = df[df["code"] == code].reset_index(drop=True)
        if len(g_full) < 25:
            continue

        day_hits = []
        low_pos_days = 0
        for d in WINDOW_DATES:
            g = g_full[g_full["date"] <= d].tail(60).reset_index(drop=True)
            if len(g) < 21:
                continue
            score, wash, detail = score_one(g)
            cat = categorize(score, wash)

            is_low, low_metrics = is_low_position(g)
            if is_low:
                low_pos_days += 1

            # 关键：同时满足"低位 + 吸筹"
            if cat == "absorb" and is_low:
                day_hits.append({
                    "date": d, "score": score, "wash": wash,
                    "detail": detail, "close": float(g["close"].iloc[-1]),
                    **low_metrics,
                })

        if not day_hits:
            continue

        latest = day_hits[-1]
        themes = pool[code]["themes"]
        targets = pool[code]["targets"]

        results.append({
            "code": code,
            "name": "",
            "targets": targets,
            "themes": themes,
            "latest_date": latest["date"],
            "latest_close": latest["close"],
            "latest_score": latest["score"],
            "latest_wash": latest["wash"],
            "latest_detail": latest["detail"],
            "latest_ret_20d": latest["ret_20d"],
            "latest_dd_high": latest["dd_high_20d"],
            "latest_bounce_low": latest["bounce_low_20d"],
            "latest_dist_ma20": latest["dist_ma20"],
            "latest_high_20d": latest["high_20d"],
            "latest_low_20d": latest["low_20d"],
            "latest_amplitude": latest.get("amplitude_ratio", 0),
            "latest_max_gain": latest.get("max_single_gain_20d", 0),
            "latest_limit_up_20d": latest.get("limit_up_count_20d", 0),
            "low_absorb_days": len(day_hits),
            "low_pos_days": low_pos_days,
            "max_wash": max((h["wash"] for h in day_hits), default=0),
            "low_absorb_dates": [h["date"] for h in day_hits],
        })

    print(f"\n评分完成，{len(results)} 只满足'低位吸筹'，耗时 {time.time()-t0:.0f}s")

    # 补名称
    try:
        df_names = pd.read_csv(ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv", dtype={"code": str})
        df_names["code"] = df_names["code"].str.zfill(6)
        c2n = dict(zip(df_names["code"], df_names["name"]))
        for r in results:
            r["name"] = c2n.get(r["code"], "")
    except Exception as exc:
        print(f"名称补全失败: {exc}")

    # 排序：低位吸筹天数 → 最大洗盘分
    results.sort(key=lambda x: (x["low_absorb_days"], x["max_wash"]), reverse=True)

    # 保存
    out_full = ROOT / "data" / "_tmp_theme_filter" / "low_position_accumulation.json"
    with open(out_full, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"完整结果: {out_full}")

    # 输出 TOP 30
    print("\n" + "=" * 130)
    print(f"{'排名':<4}{'代码':<8}{'名称':<14}{'最新日':<12}{'收盘':>8}{'20日涨':>8}"
          f"{'距高':>8}{'距低':>8}{'MA20':>7}{'规律':>6}{'洗盘':>5}{'低位吸筹天':>11}{'最大洗':>7}{'题材':<18}")
    print("-" * 130)
    for i, r in enumerate(results[:30], 1):
        name = (r.get("name") or "")[:10]
        targets = "/".join(t[:3] for t in r["targets"])[:16]
        print(f"{i:<4}{r['code']:<8}{name:<14}{r['latest_date']:<12}{r['latest_close']:>8.2f}"
              f"{r['latest_ret_20d']:>8.2f}{r['latest_dd_high']:>8.2f}{r['latest_bounce_low']:>8.2f}"
              f"{r['latest_dist_ma20']:>7.2f}{r['latest_score']:>6.1f}{r['latest_wash']:>5}"
              f"{r['low_absorb_days']:>11}{r['max_wash']:>7}  {targets}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
