# -*- coding: utf-8 -*-
"""批量测试：挑8只不同特征的股票，统计各评分档次日表现。
输出每只的 INSTITUTIONAL/SUSPECT 命中率 + 整体汇总。
"""
import sys, logging
from collections import defaultdict
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, Stock1minKline, StockFundFlow
from src.services.minute_accumulation_detector import MinuteAccumulationDetector

db = DatabaseManager()
s = db.get_session()
# 取有充足1min数据的股票（distinct + 限制价格区间覆盖）
codes_all = [r[0] for r in s.query(Stock1minKline.code).distinct().filter(
    Stock1minKline.ts >= '2026-05-01'
).limit(150).all()]
s.close()
codes_all = [c for c in codes_all if c != '600760']

# 随机挑8只，固定种子可复现
import random
random.seed(7)
codes = random.sample(codes_all, 8)
codes.sort()

det = MinuteAccumulationDetector()
START, END = "2026-04-25", "2026-06-12"

summary = []  # (code, state, score, next_chg)
per_code = []  # 每只股票的明细行

for code in codes:
    s = db.get_session()
    bars = s.query(Stock1minKline).filter(
        Stock1minKline.code == code,
        Stock1minKline.ts >= START, Stock1minKline.ts < END,
    ).order_by(Stock1minKline.ts).all()
    flows = {f.date: f for f in s.query(StockFundFlow).filter(
        StockFundFlow.code == code,
        StockFundFlow.date >= START, StockFundFlow.date <= END,
    ).all()}
    s.close()
    if not bars:
        continue
    byd = defaultdict(list)
    for b in bars: byd[b.ts.date()].append(b)
    sorted_days = sorted(byd.keys())
    dates = sorted(flows.keys())
    code_rows = []
    for i, d in enumerate(sorted_days):
        dbars = byd[d]
        if len(dbars) < det.MIN_DAY_MINUTES: continue
        f = flows.get(d)
        prev = [byd[pd] for pd in sorted_days[max(0,i-det.LOOKBACK_DAYS):i] if len(byd[pd])>=det.MIN_DAY_MINUTES]
        r = det._analyze(code, d, dbars, f, prev)
        if r is None: continue
        # 次日
        nd = None
        for fd in dates:
            if fd > d:
                nd = flows.get(fd); break
        next_chg = float(nd.pct_chg) if nd and nd.pct_chg is not None else None
        if next_chg is not None:
            summary.append((code, r.state, r.score, next_chg))
            code_rows.append((d, r.score, r.state, next_chg))
    per_code.append((code, code_rows))

# ===== 每只股票明细：只列 INSTITUTIONAL + SUSPECT =====
out = []
out.append(f"{'='*90}")
out.append(f"批量测试：8只随机股票 × {START}~{END}")
out.append(f"{'='*90}")
out.append("")
out.append("【每只股票的高分日（≥55分）及次日表现】")
for code, rows in per_code:
    high = [r for r in rows if r[1] >= 55]
    out.append(f"\n--- {code} ---")
    if not high:
        out.append("  无≥55分的日子")
        continue
    high.sort(key=lambda x: -x[1])
    for d, sc, st, nchg in high:
        mark = "✓" if nchg > 0 else "✗"
        out.append(f"  {d}  评分{sc:>3} ({st:<14}) 次日 {nchg:+6.2f}% {mark}")

# ===== 汇总：各档命中率 =====
out.append("")
out.append(f"{'='*90}")
out.append("【整体汇总：各评分档次日表现】")
out.append(f"{'='*90}")
for lo, hi, lbl in [(90,200,"INSTITUTIONAL ≥90"),
                    (75,90,"ACCUMULATE 75-89"),
                    (55,75,"SUSPECT 55-74"),
                    (0,55,"NORMAL <55")]:
    grp = [x for x in summary if lo <= x[2] < hi]
    if not grp:
        out.append(f"  {lbl:<22} n=  0")
        continue
    win = sum(1 for x in grp if x[3] > 0)
    import statistics
    avg = statistics.mean(x[3] for x in grp)
    big_win = sum(1 for x in grp if x[3] > 3)
    out.append(f"  {lbl:<22} n={len(grp):>3}  胜率={win/len(grp)*100:>4.0f}%  均涨跌={avg:+.2f}%  大涨(>3%){big_win}")

sys.stdout.write("\n".join(out) + "\n")
