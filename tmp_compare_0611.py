# -*- coding: utf-8 -*-
"""对比分析：6/11推荐股票的资金流状态 vs 6/12实际表现。"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from src.storage import DatabaseManager
from sqlalchemy import text

db = DatabaseManager()
session = db.get_session()


def load_rows_til(code, end_date):
    r = session.execute(text(
        "SELECT date, close, pct_chg, big_net, big_pct, mid_net, mid_pct, "
        "small_net, small_pct FROM stock_fund_flow "
        "WHERE code=:c AND date <= :d ORDER BY date ASC"
    ), {"c": code, "d": end_date}).fetchall()
    return [{"date": row[0], "close": row[1], "pct_chg": row[2], "big_net": row[3] or 0,
             "big_pct": row[4] or 0, "mid_net": row[5] or 0, "mid_pct": row[6] or 0,
             "small_net": row[7] or 0, "small_pct": row[8] or 0}
            for row in r]


def analyze_state(rows):
    if len(rows) < 10:
        return None
    n = len(rows)
    last = rows[-1]
    recent5 = rows[-5:]
    recent3 = rows[-3:]
    prev10 = rows[-13:-3] if n >= 13 else rows[:-3]

    big_net_5 = sum(r["big_net"] for r in recent5)
    small_net_5 = sum(r["small_net"] for r in recent5)
    price_5 = (recent5[-1]["close"] - recent5[0]["close"]) / recent5[0]["close"] * 100

    big_net_3 = sum(r["big_net"] for r in recent3)
    small_net_3 = sum(r["small_net"] for r in recent3)
    prev_big_10 = sum(r["big_net"] for r in prev10) if prev10 else 0

    big_strength = sum(r["big_pct"] for r in recent5) / 5 / 100
    retail_strength = sum(r["small_pct"] for r in recent5) / 5 / 100

    closes = [r["close"] for r in rows]
    min_p, max_p = min(closes), max(closes)
    price_position = (last["close"] - min_p) / (max_p - min_p) if max_p > min_p else 0.5

    states = {}
    states["压盘吸筹"] = (big_net_5 > 0 and small_net_5 < 0 and price_5 > -8)
    states["洗盘"] = (prev_big_10 > 0 and big_net_3 < 0 and small_net_3 < 0)
    states["高位派发"] = (price_position > 0.7 and big_net_5 < 0 and small_net_5 > 0)
    states["统计失真"] = (big_net_5 > 0 and price_5 < -15 and small_net_5 > 0)

    acc_score = (0.4 * big_strength + 0.3 * (-retail_strength) + 0.3 * (-price_5 / 10)) * 100
    acc_score = max(0, min(100, acc_score + 50))
    dist_score = (0.4 * (-big_strength) + 0.3 * retail_strength + 0.3 * price_position) * 100
    dist_score = max(0, min(100, dist_score + 50))

    total = acc_score + dist_score + 10
    prob_acc = acc_score / total * 100
    prob_dist = dist_score / total * 100

    big_dir = "大↑" if big_net_5 > 0 else "大↓"
    small_dir = "小↑" if small_net_5 > 0 else "小↓"
    active = [k for k, v in states.items() if v]

    return {
        "price": last["close"], "price_5": price_5, "price_pos": price_position,
        "big_net_5": big_net_5, "small_net_5": small_net_5,
        "big_strength": big_strength, "retail_strength": retail_strength,
        "matrix": big_dir + small_dir, "states": active,
        "prob_acc": prob_acc, "prob_dist": prob_dist,
    }


# 6/8推荐的10只
stocks = [
    ("603638", "艾迪精密"), ("000837", "秦川机床"), ("603915", "国茂股份"),
    ("002963", "豪尔赛"), ("002407", "多氟多"), ("002553", "南方精工"),
    ("600373", "中文传媒"), ("002520", "日发精机"), ("603466", "风语筑"),
    ("000795", "英洛华"),
]

# 分析截止日 = 6/8, 对比日 = 6/9
ANALYSIS_DATE = "2026-06-08"
COMPARE_DATE = "2026-06-09"

print("=" * 110)
print("{}推荐股票的资金流状态分析（截至{}数据） vs {}实际表现".format(
    ANALYSIS_DATE, ANALYSIS_DATE, COMPARE_DATE))
print("=" * 110)
print("{:<8} {:<10} {:<8} {:<10} {:<12} {:<12} {:<8} {:<8} {:<8} {:<10}".format(
    "代码", "名称", "11日价", "5日涨跌%", "5日大单(亿)", "5日小单(亿)", "结构", "吸筹%", "派发%", "12日涨跌%"))
print("-" * 110)

results = []
for code, name in stocks:
    rows_11 = load_rows_til(code, ANALYSIS_DATE)
    state = analyze_state(rows_11)

    # 对比日实际涨跌
    r12 = session.execute(text(
        "SELECT close, pct_chg FROM stock_fund_flow WHERE code=:c AND date=:d"
    ), {"c": code, "d": COMPARE_DATE}).fetchone()

    if state and r12:
        results.append((code, name, state, r12))
        print("{:<8} {:<10} {:<8.2f} {:<+9.1f} {:<14.2f} {:<14.2f} {:<8} {:<10.0f} {:<10.0f} {:<+10.2f}".format(
            code, name, state["price"], state["price_5"],
            state["big_net_5"] / 10000, state["small_net_5"] / 10000,
            state["matrix"], state["prob_acc"], state["prob_dist"], r12[1]))

print()
print("=" * 110)
print("详细状态:")
print("=" * 110)
for code, name, state, r12 in results:
    print(f"\n{code} {name}")
    print(f"  {ANALYSIS_DATE}状态: 结构={state['matrix']} 命中={state['states']}")
    print(f"  吸筹概率={state['prob_acc']:.0f}%  派发概率={state['prob_dist']:.0f}%")
    print(f"  价格位置={state['price_pos']*100:.0f}%  大单强度={state['big_strength']:+.3f}  散户强度={state['retail_strength']:+.3f}")
    print(f"  {COMPARE_DATE}实际: 收盘{r12[0]:.2f}  涨跌{r12[1]:+.2f}%")

# 汇总
print()
print("=" * 110)
print("汇总验证:")
print("=" * 110)
acc_stocks = [(c,n,s,r) for c,n,s,r in results if s["prob_acc"] > s["prob_dist"]]
dist_stocks = [(c,n,s,r) for c,n,s,r in results if s["prob_dist"] >= s["prob_acc"]]

if acc_stocks:
    avg_acc = sum(r[1] for _,_,_,r in acc_stocks) / len(acc_stocks)
    print(f"吸筹概率>派发概率 ({len(acc_stocks)}只): {COMPARE_DATE}平均涨跌 {avg_acc:+.2f}%")
    for c,n,s,r in acc_stocks:
        print(f"  {c} {n}: 吸筹{s['prob_acc']:.0f}% → {COMPARE_DATE}日{r[1]:+.2f}%")

if dist_stocks:
    avg_dist = sum(r[1] for _,_,_,r in dist_stocks) / len(dist_stocks)
    print(f"派发概率>=吸筹概率 ({len(dist_stocks)}只): {COMPARE_DATE}平均涨跌 {avg_dist:+.2f}%")
    for c,n,s,r in dist_stocks:
        print(f"  {c} {n}: 派发{s['prob_dist']:.0f}% → {COMPARE_DATE}日{r[1]:+.2f}%")

session.close()
