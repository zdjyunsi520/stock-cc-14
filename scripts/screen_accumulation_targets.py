# -*- coding: utf-8 -*-
"""三题材吸筹选股批量评分。

对候选池（金融科技 + AI应用 + 政务与行业信息化）每只股票，
在最近 20 个交易日内逐日计算规律分 + 洗盘分 + v3最终分，
统计出现吸筹特征的天数，输出排名表。

吸筹定义（v3算法）：
- 低规律(<=50) + 高洗盘(>=10)   → 主力吸筹 (v3 = score + wash)
- 高规律(>60)  + 洗盘(>0)       → 强势洗盘确认 (v3 = score + wash)
- 50<规律<=60 + 洗盘>=10        → 过热减分 (v3 = score - wash)

用户要的是"吸筹"，所以主力场景 = 低规律+高洗盘，次要 = 强势股加洗盘确认。
"""

from __future__ import annotations

import json
import os
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
LOOKBACK_DAYS = 50  # 每个评分点向前取 50 日，保证 ma20 + 30日窗口
EARLIEST = "2026-04-08"  # 50 日前


def score_one(g: pd.DataFrame) -> tuple[float, int, str, dict]:
    """复刻 PatternScreener.score_single 的评分逻辑（不含热点题材分）。"""
    if len(g) < 10:
        return 0.0, 0, "", {}

    close_s = g["close"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close_s.iloc[-1])

    ma5 = float(close_s.rolling(5).mean().iloc[-1])
    ma10 = float(close_s.rolling(10).mean().iloc[-1])
    avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
    vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0

    ret3 = float(sum(pct.iloc[-3:])) if len(pct) >= 3 else 0
    ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
    up5 = int(sum(1 for p in pct.iloc[-5:] if p > 0)) if len(pct) >= 5 else 0
    dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0

    # 规律评分（s1 热点题材分固定为 10，因为已在目标题材池内）
    s1 = 10.0
    s2 = min(vol_ratio, 3.0) * 10
    s3 = up5 * 5
    s4 = 10 if 0 <= dist_ma10 <= 5 else 0
    s5 = -5 if ret5 > 10 else 0
    score = s1 + s2 + s3 + s4 + s5

    # 洗盘分（直接调项目 static method）
    wash, wash_detail = (0, "")
    if len(g) >= 20:
        wash, wash_detail = PatternScreener._compute_wash_score(g)

    breakdown = {
        "hot_concepts": s1, "vol_ratio": round(s2, 1), "up_days": s3,
        "near_ma10": s4, "chase_penalty": s5,
        "ret5": round(ret5, 2), "ret3": round(ret3, 2),
        "dist_ma10": round(dist_ma10, 2), "vol_ratio_raw": round(vol_ratio, 2),
    }
    return round(score, 1), wash, wash_detail, breakdown


def v3_final(score: float, wash: int) -> float | None:
    if wash == 0:
        return None
    if score > 60:
        return score + wash
    if score > 50:
        return score - wash if wash >= 10 else None
    return score + wash if wash >= 10 else None


def categorize(score: float, wash: int) -> str:
    """分类：absorb=吸筹(低规律+高洗盘)，strong_wash=强势洗盘，over_hot=过热减分，excluded=排除"""
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
    print(f"时间窗口: {WINDOW_DATES[0]} ~ {WINDOW_DATES[-1]} ({len(WINDOW_DATES)} 个交易日)")

    db = DatabaseManager()
    print("加载数据库日线 ...")
    t0 = time.time()
    df_all = db.get_bulk_daily_data(days=75, end_date=WINDOW_DATES[-1])
    print(f"  加载 {len(df_all)} 行, 耗时 {time.time()-t0:.1f}s")

    # 只保留候选池股票
    df = df_all[df_all["code"].isin(pool.keys())].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"  过滤后候选股记录: {len(df)} 行, 覆盖 {df['code'].nunique()} 只")

    results = []
    t0 = time.time()
    for i, code in enumerate(sorted(pool.keys())):
        if (i + 1) % 200 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (len(pool) - i - 1)
            print(f"  进度 {i+1}/{len(pool)}, 已用 {elapsed:.0f}s, 剩 {eta:.0f}s")

        g_full = df[df["code"] == code].reset_index(drop=True)
        if len(g_full) < 20:
            continue

        # 对每个交易日 T 评分（用 T 及之前 50 日数据）
        day_hits = []
        for d in WINDOW_DATES:
            g = g_full[g_full["date"] <= d].tail(LOOKBACK_DAYS).reset_index(drop=True)
            if len(g) < 20:
                continue
            score, wash, detail, brk = score_one(g)
            final = v3_final(score, wash)
            cat = categorize(score, wash)
            if cat != "excluded":
                day_hits.append({
                    "date": d, "score": score, "wash": wash,
                    "final": final, "cat": cat, "detail": detail,
                    "close": float(g["close"].iloc[-1]),
                })

        if not day_hits:
            continue

        absorb_hits = [h for h in day_hits if h["cat"] == "absorb"]
        strong_hits = [h for h in day_hits if h["cat"] == "strong_wash"]
        overhot_hits = [h for h in day_hits if h["cat"] == "over_hot"]

        # 取最近一次有效评分
        latest = day_hits[-1]
        themes = pool[code]["themes"]
        targets = pool[code]["targets"]

        results.append({
            "code": code,
            "name": "",  # 后面补
            "targets": targets,
            "themes": themes,
            "latest_date": latest["date"],
            "latest_close": latest["close"],
            "latest_score": latest["score"],
            "latest_wash": latest["wash"],
            "latest_final": latest["final"],
            "latest_cat": latest["cat"],
            "latest_detail": latest["detail"],
            "absorb_days": len(absorb_hits),
            "strong_wash_days": len(strong_hits),
            "over_hot_days": len(overhot_hits),
            "total_hit_days": len(day_hits),
            "absorb_dates": [h["date"] for h in absorb_hits],
            "max_wash": max((h["wash"] for h in day_hits), default=0),
            "max_final": max((h["final"] for h in day_hits if h["final"]), default=0),
        })

    print(f"\n评分完成，{len(results)} 只出现吸筹/洗盘信号，耗时 {time.time()-t0:.0f}s")

    # 补股票名称
    code_name_map = {}
    try:
        session = db.get_session()
        from sqlalchemy import text
        codes = [r["code"] for r in results]
        if codes:
            rows = session.execute(text(
                "SELECT DISTINCT code, name FROM stock_daily WHERE code IN :codes"
            ).bindparams(__import__("sqlalchemy").bindparam("codes", expanding=True)), {"codes": codes}).fetchall()
            for c, n in rows:
                code_name_map[c] = n
            session.close()
    except Exception as exc:
        print(f"  名称查询失败: {exc}")
    for r in results:
        r["name"] = code_name_map.get(r["code"], "")

    # 排序：吸筹天数优先 → 强势洗盘天数 → 最大 v3 分
    results.sort(key=lambda x: (x["absorb_days"], x["strong_wash_days"], x["max_final"]), reverse=True)

    # 保存完整结果
    out_full = ROOT / "data" / "_tmp_theme_filter" / "accumulation_results.json"
    with open(out_full, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"完整结果: {out_full}")

    # 输出前 30 名摘要
    print("\n" + "=" * 120)
    print(f"{'排名':<4}{'代码':<8}{'名称':<10}{'题材':<18}{'最新日':<12}{'规律':>6}{'洗盘':>5}{'V3':>7}{'类别':<14}{'吸筹天数':>9}{'强势洗':>7}{'最大洗':>7}")
    print("-" * 120)
    cat_cn = {"absorb": "主力吸筹", "strong_wash": "强势洗盘", "over_hot": "过热减分", "excluded": "排除"}
    for i, r in enumerate(results[:40], 1):
        target_str = "/".join(t[:2] for t in r["targets"])
        print(f"{i:<4}{r['code']:<8}{r['name'][:8]:<10}{target_str[:16]:<18}{r['latest_date']:<12}"
              f"{r['latest_score']:>6.1f}{r['latest_wash']:>5}{str(r['latest_final']):>7}"
              f"{cat_cn.get(r['latest_cat'], r['latest_cat']):<14}"
              f"{r['absorb_days']:>9}{r['strong_wash_days']:>7}{r['max_wash']:>7}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
