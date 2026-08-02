# -*- coding: utf-8 -*-
"""诊断：对用户指定的误判日期，输出5个评分特征明细。
错误入选（应剔除）：000030 04-29/05-11/05-15, 000498 05-22/05-25/06-09/05-08,
                   000526 05-07/06-05, 000570 05-15
漏选正确答案（应入选）：000065 05-29, 000498 05-07/05-26,
                       000526 05-06/05-11/05-22/06-09, 000570 05-13
000065 05-22、000030 06-02/05-28 是已确认的好命中，作参照。
"""
import sys, logging
from collections import defaultdict
logging.disable(logging.CRITICAL)
from src.storage import DatabaseManager, Stock1minKline, StockFundFlow
from src.services.minute_accumulation_detector import MinuteAccumulationDetector

# 标注：F=误选(应剔除)  M=漏选(应入选)  R=参照(已知好)
CASES = [
    ("000030", "2026-04-29", "F"),
    ("000030", "2026-05-11", "F"),
    ("000030", "2026-05-15", "F"),
    ("000030", "2026-05-28", "R"),  # 已命中参照
    ("000065", "2026-05-22", "R"),  # 已命中参照(次日+7.14%)
    ("000065", "2026-05-29", "M"),
    ("000498", "2026-05-22", "F"),
    ("000498", "2026-05-25", "F"),
    ("000498", "2026-06-09", "F"),
    ("000498", "2026-05-08", "F"),
    ("000498", "2026-05-07", "M"),
    ("000498", "2026-05-26", "M"),
    ("000526", "2026-05-07", "F"),
    ("000526", "2026-06-05", "F"),
    ("000526", "2026-05-06", "M"),
    ("000526", "2026-05-11", "M"),
    ("000526", "2026-05-22", "M"),
    ("000526", "2026-06-09", "M"),
    ("000570", "2026-05-15", "F"),
    ("000570", "2026-05-13", "M"),
]

db = DatabaseManager()
det = MinuteAccumulationDetector()

# 按股票分组拉数据
codes = sorted(set(c for c, _, _ in CASES))
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
    data[code] = (byd, flows)

from datetime import datetime
def _d(s): return datetime.strptime(s, "%Y-%m-%d").date()

out = []
out.append(f"{'='*120}")
out.append("误判诊断：5特征明细（位=收盘vsPOC 衰=当日涨跌 连=连续流入 缩=缩量比 大=大单净占比）")
out.append(f"{'='*120}")
out.append(f"{'股票':<8}{'日期':<12}{'类型':<5}{'评分':>5}{'位%':>7}{'衰%':>7}{'连':>4}{'缩':>6}{'大单%':>8}{'振幅%':>7}{'收均差':>8}{'收盘位':>10}{'次日':>8}")
out.append("-"*115)

for code, date_str, typ in CASES:
    byd, flows = data[code]
    dd = _d(date_str)
    dbars = byd.get(dd, [])
    f = flows.get(dd)
    sorted_days = sorted(byd.keys())
    i = sorted_days.index(dd) if dd in sorted_days else -1
    prev = [byd[pd] for pd in sorted_days[max(0,i-5):i] if len(byd[pd])>=det.MIN_DAY_MINUTES] if i>0 else []
    r = det._analyze(code, dd, dbars, f, prev)
    if r is None:
        out.append(f"{code:<8}{date_str:<12}{typ:<5}  <无数据>")
        continue
    # 找次日
    nd = None
    for fd in sorted(flows.keys()):
        if fd > dd: nd = flows.get(fd); break
    next_chg = f"{float(nd.pct_chg):+.2f}%" if nd and nd.pct_chg is not None else "--"
    # 振幅
    prices = [b.price for b in dbars if b.price>0]
    amp = (max(prices)-min(prices))/r.price_ref*100 if r.price_ref>0 else 0
    typ_lbl = {"F":"误选", "M":"漏选", "R":"参照"}[typ]
    out.append(f"{code:<8}{date_str:<12}{typ_lbl:<5}{r.score:>5}"
               f"{r.close_vs_poc:>6.2f}%{abs(float(f.pct_chg)):>6.2f}%{r.big_consecutive:>4}"
               f"{r.shrink_ratio:>5.2f}x{r.big_pct:>+7.1f}%{amp:>6.1f}%{r.close_vs_avg:>+7.2f}%"
               f"{r.close_pos:>10}{next_chg:>8}")

# 按股票分组重排，便于横向对比
out.append("")
out.append("【按股票分组重排，便于横向对比同股内差异】")
by_code = defaultdict(list)
for code, date_str, typ in CASES:
    by_code[code].append((date_str, typ))

for code in sorted(by_code.keys()):
    out.append(f"\n--- {code} ---")
    byd, flows = data[code]
    sorted_days = sorted(byd.keys())
    for date_str, typ in sorted(by_code[code], key=lambda x: x[0]):
        dd = _d(date_str)
        dbars = byd.get(dd, [])
        f = flows.get(dd)
        i = sorted_days.index(dd) if dd in sorted_days else -1
        prev = [byd[pd] for pd in sorted_days[max(0,i-5):i] if len(byd[pd])>=det.MIN_DAY_MINUTES] if i>0 else []
        r = det._analyze(code, dd, dbars, f, prev)
        if r is None: continue
        prices = [b.price for b in dbars if b.price>0]
        amp = (max(prices)-min(prices))/r.price_ref*100 if r.price_ref>0 else 0
        nd = None
        for fd in sorted(flows.keys()):
            if fd > dd: nd = flows.get(fd); break
        next_chg = f"{float(nd.pct_chg):+.2f}%" if nd and nd.pct_chg is not None else "--"
        typ_lbl = {"F":"误选", "M":"漏选", "R":"参照"}[typ]
        out.append(f"  {date_str} [{typ_lbl}] 评分{r.score:>3} "
                   f"位(收POC){r.close_vs_poc:>5.2f}% 衰(涨跌){abs(float(f.pct_chg)):>5.2f}% "
                   f"连{r.big_consecutive} 缩{r.shrink_ratio:.2f}x 大单{r.big_pct:+.1f}% "
                   f"振幅{amp:.1f}% 收均差{r.close_vs_avg:+.2f}% → 次日{next_chg}")

sys.stdout.write("\n".join(out) + "\n")
