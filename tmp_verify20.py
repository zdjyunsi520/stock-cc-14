# -*- coding: utf-8 -*-
"""分时吸筹评分验证（固定20只样本）。

用法: python tmp_verify20.py
快速验证评分模型在这20只股票全部可用交易日上的：
  - 评分分布（各状态档样本数）
  - 评分 vs 次日涨跌的单调性（核心判据）
  - 各分项拿分率（看是否有失效项）
选股固定写死，覆盖大盘/中小盘/不同价位，便于跨次改动对比。
"""
import sys
import logging
import statistics
from collections import defaultdict

logging.disable(logging.CRITICAL)

from src.services.minute_accumulation_detector import MinuteAccumulationDetector
from src.storage import DatabaseManager, Stock1minKline, StockFundFlow

# 固定20只样本（库内有完整1min数据的股票，按数据量降序取20只）
SAMPLE_CODES = [
    "600760", "002903", "002902", "002901", "002900",
    "002899", "002897", "002896", "002895", "002893",
    "002892", "002891", "002890", "002889", "002888",
    "002887", "002886", "002885", "002884", "002882",
]

START, END = "2026-04-17", "2026-06-12"

db = DatabaseManager()
det = MinuteAccumulationDetector()

# 预加载这20只的资金流
s = db.get_session()
flows = s.query(StockFundFlow).filter(
    StockFundFlow.code.in_(SAMPLE_CODES),
    StockFundFlow.date >= START, StockFundFlow.date <= END,
).all()
s.close()

flow_by_cd = {}
dates_by_code = defaultdict(list)
for f in flows:
    flow_by_cd[(f.code, f.date)] = f
    dates_by_code[f.code].append(f.date)
for c in dates_by_code:
    dates_by_code[c].sort()

pairs = []      # (score, next_pct)
sub_scores = [] # 各分项，用于拿分率
missing = []

for code in SAMPLE_CODES:
    s = db.get_session()
    bars = s.query(Stock1minKline).filter(
        Stock1minKline.code == code,
        Stock1minKline.ts >= START, Stock1minKline.ts < END,
    ).order_by(Stock1minKline.ts).all()
    s.close()

    if not bars:
        missing.append(code)
        continue

    byd = defaultdict(list)
    for b in bars:
        byd[b.ts.date()].append(b)

    dates = dates_by_code.get(code, [])
    sorted_days = sorted(byd.keys())
    for i, d in enumerate(sorted_days):
        daybars = byd[d]
        if len(daybars) < det.MIN_DAY_MINUTES:
            continue
        f = flow_by_cd.get((code, d))
        # 找次日
        nd = None
        for fd in dates:
            if fd > d:
                nd = flow_by_cd.get((code, fd))
                break
        if not f or f.close is None or not nd or nd.pct_chg is None:
            continue
        # 前 N 个交易日的 bars（缩量/动能衰竭）
        prev_days = [byd[pd] for pd in sorted_days[max(0, i - det.LOOKBACK_DAYS):i]]
        r = det._analyze(code, d, daybars, f, prev_days)
        if r is None:
            continue
        pairs.append((r.score, r.state, float(nd.pct_chg)))
        sub_scores.append(r)

# ===== 输出报告 =====
out = []
out.append(f"样本：{len(SAMPLE_CODES)} 只固定股票 × {START}~{END}，有效 {len(pairs)} 个交易日")
if missing:
    out.append(f"（无 1min 数据的股票：{', '.join(missing)}）")
out.append("")

# 1. 评分 vs 次日涨跌（核心判据）
out.append("=== 评分 vs 次日涨跌（应有单调性）===")
for lo, hi, lbl in [(80, 101, "INSTITUTIONAL ≥80"),
                    (60, 80, "ACCUMULATE  60-79"),
                    (40, 60, "SUSPECT     40-59"),
                    (0, 40, "NORMAL      <40")]:
    grp = [x for x in pairs if lo <= x[0] < hi]
    if grp:
        win = sum(1 for x in grp if x[2] > 0) / len(grp) * 100
        avgc = statistics.mean(x[2] for x in grp)
        out.append(f"  {lbl:<22} n={len(grp):>3}  胜率={win:>4.0f}%  均涨跌={avgc:+.2f}%")
    else:
        out.append(f"  {lbl:<22} n=  0")
out.append("")

# 2. 分项拿分率（看是否有失效项：恒满或恒0）
out.append("=== 分项拿分率（理想 40-70%，过低=阈值太严，过高=白送）===")
if sub_scores:
    n = len(sub_scores)
    specs = [
        ("score_pos (收盘位60)",    "score_pos",    60),
        ("score_shrink (缩量,不评分)","score_shrink", 1),
        ("score_decay (衰竭,不评分)", "score_decay",  1),
        ("score_poc (POC 20)",      "score_poc",    20),
        ("score_va  (VA 8)",        "score_va",     8),
        ("score_tight (紧6)",       "score_tight",  6),
        ("score_range (幅6)",       "score_range",  6),
    ]
    for lbl, f, mx in specs:
        avg = sum(getattr(r, f) for r in sub_scores) / n
        out.append(f"  {lbl:<22} avg={avg:>5.2f} / {mx}  ({avg/mx*100:>3.0f}%)")
out.append("")

report = "\n".join(out)
sys.stdout.write(report + "\n")
