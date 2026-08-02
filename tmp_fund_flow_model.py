# -*- coding: utf-8 -*-
"""基于资金流状态识别模型的分析。"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from src.storage import DatabaseManager
from sqlalchemy import text

db = DatabaseManager()
session = db.get_session()


def load_rows(code):
    r = session.execute(text(
        "SELECT date, close, pct_chg, big_net, big_pct, mid_net, mid_pct, "
        "small_net, small_pct, net_flow FROM stock_fund_flow "
        "WHERE code=:c ORDER BY date ASC"
    ), {"c": code}).fetchall()
    return [{"date": row[0], "close": row[1], "pct_chg": row[2], "big_net": row[3] or 0,
             "big_pct": row[4] or 0, "mid_net": row[5] or 0, "mid_pct": row[6] or 0,
             "small_net": row[7] or 0, "small_pct": row[8] or 0, "net_flow": row[9] or 0}
            for row in r]


def analyze(code, name):
    rows = load_rows(code)
    if len(rows) < 10:
        return None
    n = len(rows)
    last = rows[-1]
    recent5 = rows[-5:]
    recent3 = rows[-3:]
    prev10 = rows[-13:-3] if n >= 13 else rows[:-3]

    big_net_5 = sum(r["big_net"] for r in recent5)
    small_net_5 = sum(r["small_net"] for r in recent5)
    mid_net_5 = sum(r["mid_net"] for r in recent5)
    price_5 = (recent5[-1]["close"] - recent5[0]["close"]) / recent5[0]["close"] * 100

    big_net_3 = sum(r["big_net"] for r in recent3)
    small_net_3 = sum(r["small_net"] for r in recent3)

    prev_big_10 = sum(r["big_net"] for r in prev10) if prev10 else 0

    big_strength = sum(r["big_pct"] for r in recent5) / 5 / 100
    retail_strength = sum(r["small_pct"] for r in recent5) / 5 / 100

    closes = [r["close"] for r in rows]
    min_p, max_p = min(closes), max(closes)
    price_position = (last["close"] - min_p) / (max_p - min_p) if max_p > min_p else 0.5

    price_eff = price_5 / (big_net_5 / 10000) if big_net_5 != 0 else 0

    states = {}
    states["压盘吸筹"] = (big_net_5 > 0 and small_net_5 < 0 and price_5 > -8)
    states["洗盘"] = (prev_big_10 > 0 and big_net_3 < 0 and small_net_3 < 0)
    states["高位派发"] = (price_position > 0.7 and big_net_5 < 0 and small_net_5 > 0)
    states["统计失真"] = (big_net_5 > 0 and price_5 < -15 and small_net_5 > 0)

    acc_score = (0.4 * big_strength + 0.3 * (-retail_strength) + 0.3 * (-price_5 / 10)) * 100
    acc_score = max(0, min(100, acc_score + 50))

    dist_score = (0.4 * (-big_strength) + 0.3 * retail_strength + 0.3 * price_position) * 100
    dist_score = max(0, min(100, dist_score + 50))

    total_score = acc_score + dist_score + 10
    prob_acc = acc_score / total_score * 100
    prob_dist = dist_score / total_score * 100
    prob_wash = 5
    prob_distort = 5
    if states["洗盘"]:
        prob_wash = 15
    if states["统计失真"]:
        prob_distort = 20

    return {
        "code": code, "name": name, "price": last["close"],
        "big_net_5": big_net_5, "small_net_5": small_net_5, "mid_net_5": mid_net_5,
        "price_5": price_5, "big_strength": big_strength, "retail_strength": retail_strength,
        "price_position": price_position, "price_eff": price_eff,
        "states": states, "acc_score": acc_score, "dist_score": dist_score,
        "prob_acc": prob_acc, "prob_dist": prob_dist, "prob_wash": prob_wash, "prob_distort": prob_distort,
        "prev_big_10": prev_big_10,
    }


codes = [("605168", "三人行"), ("600483", "福能股份"), ("000026", "飞亚达"),
         ("301280", "珠城科技"), ("300270", "中威电子"), ("605196", "华通线缆"),
         ("300504", "天邑股份"), ("603629", "利通电子")]

results = []
for code, name in codes:
    r = analyze(code, name)
    results.append(r)
    print("\n=== {} {} ===".format(code, name))
    print("  价格: {}  5日涨跌: {:+.1f}%  价格位置: {:.0f}%".format(
        r["price"], r["price_5"], r["price_position"] * 100))
    print("  5日大单: {:.2f}亿  5日小单: {:.2f}亿  5日中单: {:.2f}亿".format(
        r["big_net_5"] / 10000, r["small_net_5"] / 10000, r["mid_net_5"] / 10000))
    print("  大单强度: {:+.3f}  散户强度: {:+.3f}".format(
        r["big_strength"], r["retail_strength"]))
    print("  价格响应效率: {:.4f} (%/亿)".format(r["price_eff"]))

    big_dir = "↑" if r["big_net_5"] > 0 else "↓"
    small_dir = "↑" if r["small_net_5"] > 0 else "↓"
    matrix = {
        ("↑", "↓"): "主力接散户(吸筹)",
        ("↑", "↑"): "一致看多",
        ("↓", "↑"): "主力卖给散户(派发)",
        ("↓", "↓"): "恐慌盘",
    }
    print("  资金结构: 大单{} 小单{} → {}".format(big_dir, small_dir, matrix[(big_dir, small_dir)]))

    active = [k for k, v in r["states"].items() if v]
    print("  命中状态: {}".format(active if active else "无明确状态"))
    print("  吸筹概率: {:.0f}%  派发概率: {:.0f}%  洗盘: {}%  失真: {}%".format(
        r["prob_acc"], r["prob_dist"], r["prob_wash"], r["prob_distort"]))

session.close()
