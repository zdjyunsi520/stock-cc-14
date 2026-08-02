"""样本外回测：用上轮归纳的规则预测次日十字星

排除前 30 只，取下一批 30 只（同样按 avg_amt 分 5 档）。
对每个交易日 T+0，提取 T-1~T-5 特征，套用候选规则，输出混淆矩阵。
"""
from __future__ import annotations
import sys
import io
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 直接重新配置 stdout 为行缓冲（保留 utf-8 编码）
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except AttributeError:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

from src.storage import (
    DatabaseManager, StockDaily, StockFundFlow,
    Stock1minKline, StockDailySyncState,
)
from sqlalchemy import func, distinct
from src.services.minute_accumulation_detector import MinuteAccumulationDetector


WINDOW = 5
STRONG = 90
WEAK_LO, WEAK_HI = 75, 89
NON_HI = 55


# ---- 候选规则 ----
# 每条规则: (name, predicate(window_feats_dict)) where window_feats[0]=T-1 ... [4]=T-5
# 返回 True 表示"次日预测为强十字星"

def rule_A_strong(wf):
    """T-4 实体收敛 + 缩量 + 振幅收窄"""
    if len(wf) < 4:
        return False
    t4 = wf[3]  # T-4
    return (
        t4["body_pct"] is not None and t4["body_pct"] < 2.0
        and t4["shrink_ratio"] is not None and t4["shrink_ratio"] < 0.95
        and t4["amp"] is not None and t4["amp"] < 4.0
    )


def rule_B_strong(wf):
    """T-3 大单深度流出 + 下跌"""
    if len(wf) < 3:
        return False
    t3 = wf[2]  # T-3
    return (
        t3["big_pct"] is not None and t3["big_pct"] < -4.0
        and t3["pct_chg"] is not None and t3["pct_chg"] < -0.5
    )


def rule_AB_combined(wf):
    """A 或 B 任一满足"""
    return rule_A_strong(wf) or rule_B_strong(wf)


def rule_C_trend_down(wf):
    """T-2~T-4 累计下跌 (类似趋势底部)"""
    if len(wf) < 4:
        return False
    chgs = [wf[i]["pct_chg"] for i in [1, 2, 3] if wf[i]["pct_chg"] is not None]
    return sum(chgs) < -2.0


RULES = [
    ("A: T-4 收敛+缩量+窄幅", rule_A_strong),
    ("B: T-3 大单深流出+跌", rule_B_strong),
    ("A|B 合取", rule_AB_combined),
    ("C: T-2~4 累跌>2%", rule_C_trend_down),
]


def pick_sample(db, per_bucket=6, min_days=40, skip_buckets=6):
    """skip_buckets=N 跳过每档前 N 只（排除之前的样本）。"""
    sub_q = s_query_days(db, min_days)
    with db.session_scope() as s:
        rows = s.query(
            sub_q.c.code,
            func.avg(StockDaily.amount).label("avg_amt"),
        ).join(StockDaily, StockDaily.code == sub_q.c.code).group_by(sub_q.c.code).all()
    amts_list = sorted([(r.code, r.avg_amt or 0) for r in rows], key=lambda x: x[1])
    n = len(amts_list)
    bidx = [int(n * i / 5) for i in range(5)] + [n]
    out = []
    labels = ["冷门", "小盘", "中盘", "大盘", "超大盘"]
    for i in range(5):
        seg = amts_list[bidx[i]:bidx[i + 1]]
        # 跳过前 skip_buckets 只，取后续 per_bucket 只
        candidates = seg[skip_buckets:skip_buckets + per_bucket]
        for code, amt in candidates:
            out.append((code, None, amt, f"[{labels[i]}]"))
    # 补股票名
    with db.session_scope() as s:
        for i, (code, _, amt, lbl) in enumerate(out):
            name = s.query(StockDailySyncState.code_name).filter_by(code=code).scalar() or ""
            out[i] = (code, f"{lbl}{name}", amt)
    return out


def s_query_days(db, min_days):
    """子查询：1min 天数 >= min_days 的股票。"""
    # 注意 subquery 不能跨 session，所以这里返回 SQL 表达式（call site 需在同一 session）
    return (
        DatabaseManager().get_session().query(
            Stock1minKline.code.label("code"),
            func.count(distinct(func.date(Stock1minKline.ts))).label("days"),
        ).group_by(Stock1minKline.code).having(
            func.count(distinct(func.date(Stock1minKline.ts))) >= min_days
        ).subquery()
    )


def pick_sample_v2(db, per_bucket=6, min_days=40, skip=6):
    """两步查询避免 JOIN 大表：
    1. 单独算 1min 天数 >= min_days 的 code 集合
    2. 在 StockDaily 上按 code 算 avg amount
    """
    import time
    t0 = time.time()
    with db.session_scope() as s:
        # 步骤1: 拿到符合条件的 code 列表
        code_days = s.query(
            Stock1minKline.code,
            func.count(distinct(func.date(Stock1minKline.ts))).label("days"),
        ).group_by(Stock1minKline.code).having(
            func.count(distinct(func.date(Stock1minKline.ts))) >= min_days
        ).all()
        print(f"  步骤1: {len(code_days)} 只 >= {min_days}天, 用时{time.time()-t0:.1f}s", flush=True)

        valid_codes = [r.code for r in code_days]
        # 步骤2: 用 IN 子查询在 StockDaily 上算 avg amount
        t1 = time.time()
        amt_rows = s.query(
            StockDaily.code,
            func.avg(StockDaily.amount).label("avg_amt"),
        ).filter(StockDaily.code.in_(valid_codes)).group_by(StockDaily.code).all()
        print(f"  步骤2: avg amount 完成, 用时{time.time()-t1:.1f}s", flush=True)

        amts_list = sorted([(r.code, r.avg_amt or 0) for r in amt_rows], key=lambda x: x[1])
        n = len(amts_list)
        bidx = [int(n * i / 5) for i in range(5)] + [n]
        out = []
        labels = ["冷门", "小盘", "中盘", "大盘", "超大盘"]
        for i in range(5):
            seg = amts_list[bidx[i]:bidx[i + 1]]
            candidates = seg[skip:skip + per_bucket]
            for code, amt in candidates:
                name = s.query(StockDailySyncState.code_name).filter_by(code=code).scalar() or ""
                out.append((code, f"[{labels[i]}]{name}", amt))
        return out


def load_daily_map(db, code):
    out = {}
    with db.session_scope() as s:
        rows = s.query(StockDaily).filter_by(code=code).all()
        for r in rows:
            out[r.date] = {
                "pct_chg": r.pct_chg,
                "high": r.high,
                "low": r.low,
                "open": r.open,
                "close": r.close,
                "volume_ratio": r.volume_ratio,
                "amount": r.amount,
                "volume": r.volume,
            }
    return out


def load_fund_map(db, code):
    out = {}
    with db.session_scope() as s:
        rows = s.query(StockFundFlow).filter_by(code=code).all()
        for r in rows:
            out[r.date] = {
                "big_net": r.big_net,
                "big_pct": r.big_pct,
                "big_consecutive": r.big_consecutive,
            }
    return out


def extract_features(daily, fund, mr):
    if daily is None:
        return None
    low = daily.get("low")
    high = daily.get("high")
    amp = ((high - low) / low * 100) if low else None
    return {
        "pct_chg": daily.get("pct_chg"),
        "amp": amp,
        "volume_ratio": daily.get("volume_ratio"),
        "body_pct": mr.body_pct if mr else None,
        "wick_ratio": mr.wick_ratio if mr else None,
        "min_wick": mr.min_wick if mr else None,
        "poc_pct": mr.poc_pct if mr else None,
        "va_pct": mr.va_pct if mr else None,
        "shrink_ratio": mr.shrink_ratio if mr else None,
        "decay_ratio": mr.decay_ratio if mr else None,
        "score": mr.score if mr else None,
        "big_net": fund.get("big_net") if fund else None,
        "big_pct": fund.get("big_pct") if fund else None,
        "big_consecutive": fund.get("big_consecutive") if fund else None,
    }


def collect_window(sorted_dates, idx, daily_map, fund_map, minute_by_date):
    feats = []
    for pi in range(max(0, idx - WINDOW), idx):
        d = sorted_dates[pi]
        daily = daily_map.get(d)
        fund = fund_map.get(d)
        mr = minute_by_date.get(d)
        f = extract_features(daily, fund, mr)
        if f is None:
            return None  # 任一日缺失就放弃整窗
        feats.append(f)
    return feats if len(feats) == WINDOW else None


def main():
    db = DatabaseManager()
    det = MinuteAccumulationDetector(db=db)

    sample = pick_sample_v2(db, per_bucket=6, min_days=40, skip=6)
    print(f"抽样: {len(sample)} 只新股票（跳过前6）", flush=True)
    for code, name, amt in sample[:5]:
        print(f"  {code} {name} avg={amt/1e8:.2f}亿", flush=True)
    print(f"  ... 共 {len(sample)} 只\n", flush=True)

    # 真实标签 vs 规则命中
    # 规则 → 命中的样本中 (强/弱/非) 计数
    rule_hits = {r[0]: {"强≥90": 0, "弱75-89": 0, "非<55": 0, "中间55-74": 0} for r in RULES}
    # 每个真实组里被规则命中的样本数（用于召回率）
    rule_by_truth = {r[0]: defaultdict(int) for r in RULES}
    # 总样本数
    totals = {"强≥90": 0, "弱75-89": 0, "非<55": 0, "中间55-74": 0}

    # 列举具体命中案例
    rule_examples = {r[0]: [] for r in RULES}

    for code, name, avg_amt in sample:
        print(f"处理: {code} {name}", flush=True)
        with db.session_scope() as s:
            row = s.query(
                func.min(Stock1minKline.ts).label("mn"),
                func.max(Stock1minKline.ts).label("mx"),
            ).filter_by(code=code).first()
            if not row or not row.mn:
                continue
            start = row.mn.date()
            end = row.mx.date()

        results = det.detect(code, start, end)
        if not results:
            continue
        print(f"  评分天数: {len(results)}", flush=True)
        minute_by_date = {r.date: r for r in results}
        sorted_dates = [r.date for r in results]
        daily_map = load_daily_map(db, code)
        fund_map = load_fund_map(db, code)

        for i, d in enumerate(sorted_dates):
            r = minute_by_date[d]
            sc = r.score
            if sc >= STRONG:
                grp = "强≥90"
            elif WEAK_LO <= sc <= WEAK_HI:
                grp = "弱75-89"
            elif sc < NON_HI:
                grp = "非<55"
            else:
                grp = "中间55-74"
            totals[grp] += 1

            wf = collect_window(sorted_dates, i, daily_map, fund_map, minute_by_date)
            if wf is None:
                continue

            for rname, rfn in RULES:
                if rfn(wf):
                    rule_hits[rname][grp] += 1
                    rule_by_truth[rname][grp] += 1
                    if len(rule_examples[rname]) < 10:
                        rule_examples[rname].append({
                            "code": code, "name": name, "date": d,
                            "score": sc, "grp": grp,
                            "t4_body": wf[3]["body_pct"],
                            "t4_shrink": wf[3]["shrink_ratio"],
                            "t4_amp": wf[3]["amp"],
                            "t3_big_pct": wf[2]["big_pct"],
                            "t3_chg": wf[2]["pct_chg"],
                        })

    # 输出
    print("=" * 90)
    print("样本外回测：每条规则在不同真实组上的命中分布")
    print("=" * 90)
    print(f"真实分布: " + " ".join(f"{g}={n}" for g, n in totals.items()))

    for rname, _ in RULES:
        print(f"\n--- 规则: {rname} ---")
        h = rule_hits[rname]
        total_hit = sum(h.values())
        if total_hit == 0:
            print("  无命中")
            continue
        print(f"  总命中: {total_hit}")
        for g in ["强≥90", "弱75-89", "非<55", "中间55-74"]:
            hit = h[g]
            tot = totals[g]
            prec = hit / total_hit * 100 if total_hit else 0
            rec = hit / tot * 100 if tot else 0
            print(f"  {g:12s}  命中={hit:4d} / 总={tot:4d}  精确率={prec:5.1f}%  召回率={rec:5.1f}%")
        # 命中强档（≥75）的精确率
        strong_weak = h["强≥90"] + h["弱75-89"]
        if total_hit:
            print(f"  >>> 十字星(≥75)精确率 = {strong_weak}/{total_hit} = {strong_weak/total_hit*100:.1f}%")
        # 命中强档（≥90）的精确率
        if total_hit:
            print(f"  >>> 真十字星(≥90)精确率 = {h['强≥90']}/{total_hit} = {h['强≥90']/total_hit*100:.1f}%")

    # 输出部分命中案例
    print("\n" + "=" * 90)
    print("命中案例（前 10）")
    print("=" * 90)
    for rname, _ in RULES:
        print(f"\n--- {rname} ---")
        for ex in rule_examples[rname]:
            print(
                f"  {ex['code']} {ex['name']:14s} {ex['date']}  实际={ex['grp']:8s} 评分={ex['score']:3d}  "
                f"T-4实体={ex['t4_body']:.2f}% T-4缩量={ex['t4_shrink']:.2f} T-4振幅={ex['t4_amp']:.2f}%  "
                f"T-3大单%={ex['t3_big_pct']:.2f} T-3涨跌={ex['t3_chg']:.2f}%"
            )


if __name__ == "__main__":
    main()
