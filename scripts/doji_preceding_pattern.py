"""十字星前兆对照研究

对若干股票，识别每个交易日是否为十字星（评分>=90 / 75-89 / <55），
然后对每个十字星日 T+0，提取 T-1~T-5 的日线/分时/资金特征，
对比"前5天分布"在十字星组 vs 非十字星组上的差异。

输出：纯事实表格，无主观结论（用户偏好）。
"""
from __future__ import annotations
import sys
import io
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta

# 让脚本能从项目根导入 src.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from src.storage import DatabaseManager, StockDaily, StockFundFlow
from src.services.minute_accumulation_detector import MinuteAccumulationDetector


# ---- 配置 ----
WINDOW = 5                # T-1 ~ T-5
STRONG = 90               # 强十字星阈值
WEAK_LO, WEAK_HI = 75, 89 # 弱十字星
NON_HI = 55               # 非十字星上限（<55）


def pick_sample(db, per_bucket=6, min_days=40):
    """按平均日成交额分 5 档，每档取 per_bucket 只股票。

    返回 List[(code, name, avg_amount)]。
    """
    from src.storage import Stock1minKline, StockDailySyncState
    from sqlalchemy import func, distinct
    with db.session_scope() as s:
        # 先选 1min 天数 >= min_days 的股票
        sub = s.query(
            Stock1minKline.code.label("code"),
            func.count(distinct(func.date(Stock1minKline.ts))).label("days"),
        ).group_by(Stock1minKline.code).having(
            func.count(distinct(func.date(Stock1minKline.ts))) >= min_days
        ).subquery()

        # 联表 StockDaily 算 avg amount
        rows = s.query(
            sub.c.code,
            func.avg(StockDaily.amount).label("avg_amt"),
        ).join(StockDaily, StockDaily.code == sub.c.code).group_by(sub.c.code).all()
        if not rows:
            return []
        amts = sorted([r.avg_amt or 0 for r in rows])
        n = len(amts)
        # 切 5 档：边界取 p0,p20,p40,p60,p80,p100（最后一个是 amts[-1]+1）
        bidx = [int(n * i / 5) for i in range(5)] + [n]
        boundaries = [amts[bidx[i]] if bidx[i] < n else (amts[-1] + 1) for i in range(6)]
        buckets = {i: [] for i in range(5)}
        for r in rows:
            amt = r.avg_amt or 0
            for i in range(5):
                if boundaries[i] <= amt < boundaries[i + 1] or (i == 4 and amt >= boundaries[i]):
                    if len(buckets[i]) < per_bucket:
                        buckets[i].append((r.code, amt))
                    break
        out = []
        labels = ["冷门", "小盘", "中盘", "大盘", "超大盘"]
        for i in range(5):
            for code, amt in buckets[i]:
                name = s.query(StockDailySyncState.code_name).filter_by(code=code).scalar() or ""
                out.append((code, f"[{labels[i]}]{name}", amt))
        return out


def load_daily_map(db, code):
    """返回 {date: dict}，全表转 dict 避免脱 session。"""
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
    """返回 {date: dict}。"""
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


def extract_features(daily, fund, minute_res):
    """从单日数据提取特征。返回 dict 或 None。"""
    if daily is None:
        return None
    # 日线
    low = daily.get("low")
    high = daily.get("high")
    amp = ((high - low) / low * 100) if low else None
    feat = {
        # 日线
        "pct_chg": daily.get("pct_chg"),
        "amp": amp,
        "volume_ratio": daily.get("volume_ratio"),
        # 分时（来自 AccumulationResult）
        "body_pct": minute_res.body_pct if minute_res else None,
        "wick_ratio": minute_res.wick_ratio if minute_res else None,
        "min_wick": minute_res.min_wick if minute_res else None,
        "poc_pct": minute_res.poc_pct if minute_res else None,
        "va_pct": minute_res.va_pct if minute_res else None,
        "range_pct": minute_res.range_pct if minute_res else None,
        "shrink_ratio": minute_res.shrink_ratio if minute_res else None,
        "decay_ratio": minute_res.decay_ratio if minute_res else None,
        "score": minute_res.score if minute_res else None,
        # 资金
        "big_net": fund.get("big_net") if fund else None,
        "big_pct": fund.get("big_pct") if fund else None,
        "big_consecutive": fund.get("big_consecutive") if fund else None,
    }
    return feat


def collect_window(sorted_dates, idx, daily_map, fund_map, minute_by_date, window=WINDOW):
    """对 sorted_dates[idx] 这一天（=T+0），返回前 window 天的特征列表（T-1 在前）。"""
    feats = []
    # 找到 idx 之前的 window 个交易日
    prev_idx = list(range(max(0, idx - window), idx))
    for pi in prev_idx:
        d = sorted_dates[pi]
        daily = daily_map.get(d)
        fund = fund_map.get(d)
        mr = minute_by_date.get(d)
        f = extract_features(daily, fund, mr)
        if f is None:
            continue
        feats.append(f)
    return feats


def stats_by_offset(buckets, offset, field):
    """从 buckets 提取 offset (0=T-1, 4=T-5) 上 field 的统计。
    buckets: List[Tuple[group_name, List[window_feats]]]，window_feats 是 list of dict
    返回 {group: (n, mean, median)}。
    """
    out = {}
    for gname, windows in buckets.items():
        vals = []
        for wf in windows:
            if offset < len(wf):
                v = wf[offset].get(field)
                if v is not None:
                    vals.append(v)
        if vals:
            out[gname] = (len(vals), statistics.mean(vals), statistics.median(vals))
        else:
            out[gname] = (0, None, None)
    return out


def fmt(v, suffix="", prec=2):
    if v is None:
        return "—"
    return f"{v:.{prec}f}{suffix}"


def main():
    db = DatabaseManager()
    det = MinuteAccumulationDetector(db=db)

    sample = pick_sample(db, per_bucket=6, min_days=40)
    print(f"抽样: {len(sample)} 只股票")
    WATCH = sample

    # 分组样本：{group: List[window_feats]}
    # 其中 window_feats 是该十字星/非十字星日前 WINDOW 天的特征列表
    groups = {
        "强≥90": defaultdict(list),  # group -> offset_idx -> [vals]
        "弱75-89": defaultdict(list),
        "非<55": defaultdict(list),
    }
    # 我们直接收集所有 window_feats 列表，再分组求统计
    raw = {"强≥90": [], "弱75-89": [], "非<55": []}

    for code, name, avg_amt in WATCH:
        print(f"\n=== {code} {name} (avg_amt={avg_amt/1e8:.2f}亿) ===")
        # 获取全部1min范围
        with db.session_scope() as s:
            from src.storage import Stock1minKline
            from sqlalchemy import func
            row = s.query(
                func.min(Stock1minKline.ts).label("mn"),
                func.max(Stock1minKline.ts).label("mx"),
            ).filter_by(code=code).first()
            if not row or not row.mn:
                print("  无 1min 数据")
                continue
            start = row.mn.date()
            end = row.mx.date()
        print(f"  区间 {start} ~ {end}")

        # 调 detect 获取全历史评分
        results = det.detect(code, start, end)
        if not results:
            print("  detect 返回空")
            continue
        minute_by_date = {r.date: r for r in results}
        sorted_dates = [r.date for r in results]
        date_idx = {d: i for i, d in enumerate(sorted_dates)}

        # 加载日线/资金
        daily_map = load_daily_map(db, code)
        fund_map = load_fund_map(db, code)

        # 按当日 score 分组，收集前5天特征
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
                continue  # 落在 55-74 之间的"中间态"不计入任一组

            wf = collect_window(sorted_dates, i, daily_map, fund_map, minute_by_date)
            if len(wf) == WINDOW:
                raw[grp].append(wf)

        # 每只股票的当日分布
        cnt = {"强≥90": 0, "弱75-89": 0, "非<55": 0}
        for r in results:
            sc = r.score
            if sc >= STRONG:
                cnt["强≥90"] += 1
            elif WEAK_LO <= sc <= WEAK_HI:
                cnt["弱75-89"] += 1
            elif sc < NON_HI:
                cnt["非<55"] += 1
        print(f"  评分分布: 强≥90={cnt['强≥90']} 弱75-89={cnt['弱75-89']} 非<55={cnt['非<55']}")

    # 汇总
    print("\n\n" + "=" * 90)
    print("对照研究汇总（前5天 T-1~T-5 的特征 vs 当日 T+0 的十字星状态）")
    print("=" * 90)
    print(f"样本数: 强≥90={len(raw['强≥90'])} 弱75-89={len(raw['弱75-89'])} 非<55={len(raw['非<55'])}")

    # 对每个 (字段, offset) 输出三组均值/中位数
    FIELDS = [
        ("pct_chg", "T 日线涨跌%", 2),
        ("amp", "T 日线振幅%", 2),
        ("volume_ratio", "T 日线量比", 2),
        ("score", "T 分时评分", 0),
        ("body_pct", "T 实体占价格%", 3),
        ("wick_ratio", "T 影线占振幅%", 1),
        ("min_wick", "T 双侧较小影线%", 3),
        ("poc_pct", "T POC占比%", 2),
        ("va_pct", "T VA占比%", 2),
        ("shrink_ratio", "T 量比(分时)", 2),
        ("decay_ratio", "T 动能衰竭", 2),
        ("big_net", "T 大单净额(万)", 0),
        ("big_pct", "T 大单净占比%", 2),
        ("big_consecutive", "T 大单连续", 0),
    ]
    OFFSETS = list(range(WINDOW))  # 0=T-1, 4=T-5

    for field, label, prec in FIELDS:
        print(f"\n--- {label} ({field}) ---")
        header = f"{'offset':8s} | {'强≥90 mean/median':25s} | {'弱75-89 mean/median':25s} | {'非<55 mean/median':25s}"
        print(header)
        print("-" * len(header))
        for off in OFFSETS:
            row = f"T-{off+1:<5d} | "
            for grp in ["强≥90", "弱75-89", "非<55"]:
                vals = []
                for wf in raw[grp]:
                    if off < len(wf):
                        v = wf[off].get(field)
                        if v is not None:
                            vals.append(v)
                if vals:
                    m = statistics.mean(vals)
                    med = statistics.median(vals)
                    cell = f"{m:.{prec}f}/{med:.{prec}f} (n={len(vals)})"
                else:
                    cell = "—"
                row += f"{cell:25s} | "
            print(row)

    print("\n（结束）")


if __name__ == "__main__":
    main()
