# -*- coding: utf-8 -*-
"""验证十字星v3公式（实体40+影线30+双侧30）的分离度。"""
import sys, logging
from collections import defaultdict
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, Stock1minKline, StockFundFlow
from src.services.minute_accumulation_detector import MinuteAccumulationDetector

ANSWERS = [
    ("600760", "2026-04-30"), ("600760", "2026-05-21"), ("600760", "2026-05-28"),
    ("600760", "2026-05-29"), ("600760", "2026-06-10"),
    ("000157", "2026-05-06"), ("000157", "2026-05-20"), ("000157", "2026-05-21"),
    ("000157", "2026-05-22"), ("000157", "2026-05-26"), ("000157", "2026-05-28"), ("000157", "2026-05-29"),
    ("000988", "2026-05-22"), ("000988", "2026-06-03"),  # 新增要保
]
NEG = [
    ("000157", "2026-05-07"), ("000157", "2026-05-13"), ("000157", "2026-05-19"),
    ("000988", "2026-06-05"),  # 误选
]

db = DatabaseManager()
det = MinuteAccumulationDetector()
codes = sorted(set(c for c, _ in ANSWERS + NEG))
data = {}
for code in codes:
    s = db.get_session()
    bars = s.query(Stock1minKline).filter(
        Stock1minKline.code == code,
        Stock1minKline.ts >= "2026-04-20", Stock1minKline.ts < "2026-06-13",
    ).order_by(Stock1minKline.ts).all()
    flows = {f.date: f for f in s.query(StockFundFlow).filter(
        StockFundFlow.code == code,
        StockFundFlow.date >= "2026-04-20", StockFundFlow.date <= "2026-06-12",
    ).all()}
    s.close()
    byd = defaultdict(list)
    for b in bars: byd[b.ts.date()].append(b)
    data[code] = (byd, flows, sorted(byd.keys()))

from datetime import datetime
def _d(s): return datetime.strptime(s, "%Y-%m-%d").date()

def analyze(code, d_str):
    byd, flows, sorted_days = data[code]
    dd = _d(d_str)
    dbars = byd.get(dd, [])
    f = flows.get(dd)
    if not dbars or not f: return None
    i = sorted_days.index(dd) if dd in sorted_days else -1
    prev = [byd[pd] for pd in sorted_days[max(0,i-5):i] if len(byd[pd])>=det.MIN_DAY_MINUTES] if i>0 else []
    return det._analyze(code, dd, dbars, f, prev)

out = ["="*110, "十字星v3公式（实体40+影线30+双侧30）分离度验证", "="*110]
out.append(f"{'股票':<8}{'日期':<12}{'类型':<6}{'评分':>5} (体/影/双){'实体%':>8}{'影线%':>6}{'双侧%':>7}")
out.append("-"*80)
ans_scores, neg_scores = [], []
for code, d in ANSWERS:
    r = analyze(code, d)
    if r is None: continue
    out.append(f"{code:<8}{d:<12}{'答案':<6}{r.score:>5} ({r.score_pos:>2}/{r.score_decay:>2}/{r.score_consec:>2})"
               f"{r.body_pct:>7.2f}%{r.wick_ratio:>5.0f}%{r.min_wick:>6.2f}%")
    ans_scores.append((d, r.score))
for code, d in NEG:
    r = analyze(code, d)
    if r is None: continue
    out.append(f"{code:<8}{d:<12}{'误选':<6}{r.score:>5} ({r.score_pos:>2}/{r.score_decay:>2}/{r.score_consec:>2})"
               f"{r.body_pct:>7.2f}%{r.wick_ratio:>5.0f}%{r.min_wick:>6.2f}%")
    neg_scores.append((d, r.score))

out.append(f"\n答案评分: {sorted(ans_scores, key=lambda x:-x[1])}")
out.append(f"误选评分: {sorted(neg_scores, key=lambda x:-x[1])}")
ans_min = min(s for _,s in ans_scores); neg_max = max(s for _,s in neg_scores)
out.append(f"答案最低 {ans_min} vs 误选最高 {neg_max} → {'✓分离' if ans_min > neg_max else '✗重叠'}")
sys.stdout.write("\n".join(out) + "\n")
