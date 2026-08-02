# -*- coding: utf-8 -*-
"""预测明天是否出十字星：正负样本对比，找出有区分度的前兆特征。
正样本 = 次日是十字星(评分≥75) 的日子，看它的前5天
负样本 = 次日不是十字星 的日子，看它的前5天
对比两组在每个特征上的分布，找出正样本独有/显著的特征。
输出到 十字星前兆规律-预测.md
"""
import sys, logging, statistics
from collections import defaultdict
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, Stock1minKline, StockFundFlow
from src.services.minute_accumulation_detector import MinuteAccumulationDetector

START, END = "2026-04-17", "2026-06-12"
THRESHOLD = 75  # 十字星判定

db = DatabaseManager()
det = MinuteAccumulationDetector()

s = db.get_session()
codes = [r[0] for r in s.query(Stock1minKline.code).filter(
    Stock1minKline.ts >= "2026-05-01"
).distinct().limit(60).all()]
s.close()
import random
random.seed(2024)
random.shuffle(codes)
codes = codes[:20]
codes.sort()

def day_feat(d, byd, flows):
    dbars = byd.get(d, [])
    f = flows.get(d)
    if not dbars or not f or f.pct_chg is None or f.close is None: return None
    prices = [b.price for b in dbars if b.price > 0]
    if not prices: return None
    open_p = prices[0]; close_p = f.close
    high = max(prices); low = min(prices)
    vol = sum(b.volume for b in dbars if b.volume > 0)
    amt = sum(b.amount for b in dbars if b.amount > 0)
    avg = amt / vol if vol > 0 else close_p
    body = abs(close_p - open_p)
    body_pct = body / open_p * 100 if open_p > 0 else 0
    chg = float(f.pct_chg)
    amp = (high - low) / avg * 100 if avg > 0 else 0
    upper = (high - max(open_p, close_p)) / open_p * 100 if open_p > 0 else 0
    lower = (min(open_p, close_p) - low) / open_p * 100 if open_p > 0 else 0
    yin_yang = "阴" if close_p < open_p else ("阳" if close_p > open_p else "平")
    return dict(chg=chg, body_pct=body_pct, amp=amp, upper=upper, lower=lower,
                yin_yang=yin_yang, vol=vol)

# 收集所有 (前5天特征, 次日是否十字星)
records = []  # (前5天特征列表, is_doji_next)
for code in codes:
    s = db.get_session()
    bars = s.query(Stock1minKline).filter(
        Stock1minKline.code == code, Stock1minKline.ts >= START, Stock1minKline.ts < END,
    ).order_by(Stock1minKline.ts).all()
    flows = {f.date: f for f in s.query(StockFundFlow).filter(
        StockFundFlow.code == code, StockFundFlow.date >= START, StockFundFlow.date <= END,
    ).all()}
    s.close()
    byd = defaultdict(list)
    for b in bars: byd[b.ts.date()].append(b)
    sorted_days = sorted(byd.keys())
    # 预算每天的十字星评分
    day_is_doji = {}
    for i, d in enumerate(sorted_days):
        dbars = byd[d]
        if len(dbars) < det.MIN_DAY_MINUTES: continue
        f = flows.get(d)
        prev = [byd[pd] for pd in sorted_days[max(0,i-5):i] if len(byd[pd])>=det.MIN_DAY_MINUTES]
        r = det._analyze(code, d, dbars, f, prev)
        if r: day_is_doji[d] = r.score >= THRESHOLD
    # 对每个有前5天数据的日，记录 (前5天特征, 次日是否十字星)
    for i, d in enumerate(sorted_days):
        if i < 5 or i >= len(sorted_days)-1: continue
        next_d = sorted_days[i+1]
        if next_d not in day_is_doji: continue
        preceding = []
        for j in range(5):
            pd = sorted_days[i-5+j]
            pf = day_feat(pd, byd, flows)
            if pf is None: break
            preceding.append(pf)
        if len(preceding) < 5: continue
        records.append((preceding, day_is_doji[next_d], code, str(d)))

pos = [r for r in records if r[1]]  # 次日是十字星
neg = [r for r in records if not r[1]]  # 次日不是

n_pos, n_neg = len(pos), len(neg)
out = []
out.append("# 十字星前兆规律 - 预测（明天是否出十字星）\n")
out.append(f"**正样本**（次日是十字星）: {n_pos} 个")
out.append(f"**负样本**（次日不是十字星）: {n_neg} 个")
out.append(f"**正样本占比**: {n_pos/(n_pos+n_neg)*100:.1f}%\n")

def p10(x): return sorted(x)[max(0,int(len(x)*0.1)-1)] if x else 0
def p50(x): return statistics.median(x) if x else 0
def p90(x): return sorted(x)[min(len(x)-1,int(len(x)*0.9))] if x else 0

# 对比每个前兆特征
out.append("## 正负样本前兆特征对比\n")
out.append("（看哪个特征正样本和负样本分布差异大 → 有区分度）\n")
out.append("| 特征 | 正样本p50 | 负样本p50 | 正p10 | 正p90 | 负p10 | 负p90 | 区分度 |")
out.append("|---|---|---|---|---|---|---|---|")

def cmp_feature(name, pos_vals, neg_vals):
    p_med = statistics.median(pos_vals); n_med = statistics.median(neg_vals)
    p_lo, p_hi = p10(pos_vals), p90(pos_vals)
    n_lo, n_hi = p10(neg_vals), p90(neg_vals)
    # 区分度：p50是否在负样本p10-p90外
    if p_med < n_lo or p_med > n_hi:
        disc = "★★强"
    elif abs(p_med - n_med) > abs(statistics.stdev(neg_vals)) if len(neg_vals)>1 else False:
        disc = "★中"
    else:
        disc = "弱"
    out.append(f"| {name} | {p_med:.2f} | {n_med:.2f} | {p_lo:.2f} | {p_hi:.2f} | {n_lo:.2f} | {n_hi:.2f} | {disc} |")

# D-1(前1天)特征
d1_chg_pos = [r[0][-1]["chg"] for r in pos]
d1_chg_neg = [r[0][-1]["chg"] for r in neg]
cmp_feature("D-1涨跌%", d1_chg_pos, d1_chg_neg)

d1_body_pos = [r[0][-1]["body_pct"] for r in pos]
d1_body_neg = [r[0][-1]["body_pct"] for r in neg]
cmp_feature("D-1实体%", d1_body_pos, d1_body_neg)

d1_amp_pos = [r[0][-1]["amp"] for r in pos]
d1_amp_neg = [r[0][-1]["amp"] for r in neg]
cmp_feature("D-1振幅%", d1_amp_pos, d1_amp_neg)

d1_lower_pos = [r[0][-1]["lower"] for r in pos]
d1_lower_neg = [r[0][-1]["lower"] for r in neg]
cmp_feature("D-1下影%", d1_lower_pos, d1_lower_neg)

d1_upper_pos = [r[0][-1]["upper"] for r in pos]
d1_upper_neg = [r[0][-1]["upper"] for r in neg]
cmp_feature("D-1上影%", d1_upper_pos, d1_upper_neg)

# 前5日累计
cum5_pos = [sum(p["chg"] for p in r[0]) for r in pos]
cum5_neg = [sum(p["chg"] for p in r[0]) for r in neg]
cmp_feature("前5日累计涨跌%", cum5_pos, cum5_neg)

# 前5日阴线数
yin_count_pos = [sum(1 for p in r[0] if p["yin_yang"]=="阴") for r in pos]
yin_count_neg = [sum(1 for p in r[0] if p["yin_yang"]=="阴") for r in neg]
cmp_feature("前5日阴线数", yin_count_pos, yin_count_neg)

# 前5日平均振幅
avg_amp_pos = [statistics.mean([p["amp"] for p in r[0]]) for r in pos]
avg_amp_neg = [statistics.mean([p["amp"] for p in r[0]]) for r in neg]
cmp_feature("前5日均振幅%", avg_amp_pos, avg_amp_neg)

# 前5日最大跌幅（单日）
max_drop_pos = [min(p["chg"] for p in r[0]) for r in pos]
max_drop_neg = [min(p["chg"] for p in r[0]) for r in neg]
cmp_feature("前5日最大单日跌幅%", max_drop_pos, max_drop_neg)

# D-1量比(D-1/D-5)
vol_ratio_pos = [r[0][-1]["vol"]/r[0][0]["vol"] for r in pos if r[0][0]["vol"]>0]
vol_ratio_neg = [r[0][-1]["vol"]/r[0][0]["vol"] for r in neg if r[0][0]["vol"]>0]
cmp_feature("D-1/D-5量比", vol_ratio_pos, vol_ratio_neg)

# D-1实体是否已是小实体（星形）
d1_is_small_pos = [1 if r[0][-1]["body_pct"]<0.5 else 0 for r in pos]
d1_is_small_neg = [1 if r[0][-1]["body_pct"]<0.5 else 0 for r in neg]
out.append("")
out.append("## D-1已是小实体(星)的概率\n")
out.append(f"- 正样本(次日十字星): D-1实体<0.5% 占 {sum(d1_is_small_pos)}/{n_pos} ({sum(d1_is_small_pos)/n_pos*100:.0f}%)")
out.append(f"- 负样本(次日非十字星): D-1实体<0.5% 占 {sum(d1_is_small_neg)}/{n_neg} ({sum(d1_is_small_neg)/n_neg*100:.0f}%)")
out.append("")

# 阴线连续数（D-1往前连续阴线数）
def consec_yin(prec):
    c = 0
    for p in reversed(prec):
        if p["yin_yang"]=="阴": c+=1
        else: break
    return c
consec_pos = [consec_yin(r[0]) for r in pos]
consec_neg = [consec_yin(r[0]) for r in neg]
cmp_feature("D-1往前连续阴线数", consec_pos, consec_neg)

out.append("")
out.append("## 样本明细（正样本：次日出十字星的前5天）\n")
out.append("| 股票 | 当日 | D-5 | D-4 | D-3 | D-2 | D-1 | 累计 |")
out.append("|---|---|---|---|---|---|---|---|")
for prec, _, code, d in pos:
    seq = " | ".join(f"{p['chg']:+.1f}%({p['yin_yang']})" for p in prec)
    cum = sum(p["chg"] for p in prec)
    out.append(f"| {code} | {d} | " + " | ".join(f"{p['chg']:+.1f}%" for p in prec) + f" | {cum:+.1f}% |")

report = "\n".join(out)
with open("十字星前兆规律-预测.md","w",encoding="utf-8") as fp:
    fp.write(report)
sys.stdout.write(report + "\n")
sys.stderr.write(f"\n已写入 十字星前兆规律-预测.md（正{n_pos} 负{n_neg}）\n")
