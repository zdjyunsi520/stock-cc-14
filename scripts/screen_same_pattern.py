# -*- coding: utf-8 -*-
"""相同规律选股 + T+N 回测 —— 基于 06-15/16/17/18/22 五天 94 只推送股共性规律。

修正后的 6 条信号规则（去 close_pos，pct 放宽到 +1%，vol_ratio 用绝对值）：
  T-1 日（信号前一交易日）：
    ① dist_ma10 ∈ [-5%, +5%]              贴近 MA10
    ② wash_score ≥ 1                       已出现洗盘特征
    ③ ret5 ≤ +15%                          未明显拉升
  T 日（信号当日）：
    ④ pct_chg ≥ +1%                        上涨（温和日也能入选）
    ⑤ vol_ratio ≥ 1.1                      量能放大（绝对阈值）
    ⑥ dist_ma10 Δ ≥ +2%                    加速向上

用法：
  # 扫 06-22 单日（默认行为），输出当天符合规律的股票名单
  python scripts/screen_same_pattern.py
  python scripts/screen_same_pattern.py --date 2026-06-19

  # 5 天 / 22 天历史窗口回测，对命中股票做 T+N 统计
  python scripts/screen_same_pattern.py --backtest
  python scripts/screen_same_pattern.py --backtest --forward-days 1 3 5 7

  # 自定义多日（既出名单又回测）
  python scripts/screen_same_pattern.py --dates 2026-06-15 2026-06-22

输出：
  - 控制台：当天/多日命中股票明细表 + T+N 均值/中位/胜率
  - JSON：data/_tmp_theme_filter/same_pattern_hits.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

# ---- 修正后的 6 条规则默认阈值 ----
DIST_MA10_T1_MIN, DIST_MA10_T1_MAX = -5.0, 5.0       # ① 贴 MA10
WASH_T1_MIN = 1                                       # ② 至少 1 分洗盘特征
RET5_T1_MAX = 15.0                                    # ③ 未明显拉升
PCT_T_MIN = 1.0                                       # ④ T 日上涨（修正：3→1）
VOL_RATIO_T_MIN = 1.1                                 # ⑤ T 日量比绝对值（修正：Δ0.2→绝对1.1）
DIST_MA10_DELTA_MIN = 2.0                             # ⑥ dist_ma10 加速向上

# ---- 可选第 7 条：close_pos 强约束 ----
CLOSE_POS_MIN = 0.8                                   # ⑦ 可选：T 日收高位

# ---- 可选资金面 3 条（基于 TOP vs BOT 资金流分析得出）----
T1_MAIN_NET_MIN = 500.0                               # ⑧ T-1 主力净流入 ≥ 500 万
T_MAIN5D_MIN, T_MAIN5D_MAX = -2000.0, 500.0           # ⑨ T 日 5日主力累计 ∈ [-2000, +500]
T_SMALL_NET_MAX = -500.0                              # ⑩ T 日小单净流入 ≤ -500 万

# ---- 默认回测窗口 ----
DEFAULT_BACKTEST_DATES = [
    "2026-05-22", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28",
    "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
    "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11",
    "2026-06-12", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
    "2026-06-19", "2026-06-22",
]
DEFAULT_FORWARD_DAYS = list(range(1, 11))
DEFAULT_SINGLE_DATE = "2026-06-22"


def is_excluded_board(code: str) -> bool:
    c = code.strip()
    if c.startswith("30"): return True
    if c.startswith("688") or c.startswith("689"): return True
    if c.startswith("8") or c.startswith("4") or c.startswith("920"): return True
    return False


def load_universe() -> dict:
    csv = ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv"
    df = pd.read_csv(csv, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    df["name"] = df["name"].astype(str).str.strip()
    pool = {}
    for _, row in df.iterrows():
        code, name = row["code"], row["name"]
        if is_excluded_board(code): continue
        if "ST" in name.upper() or "退" in name: continue
        pool[code] = name
    return pool


def load_hot_pool(date_iso: str, universe: dict) -> dict:
    """加载某天的热点题材股票池（从 pattern_cache + concept_cache 构建）。

    结构：
      pattern_cache/pattern_YYYYMMDD.json -> hot_themes: [[theme, weight], ...]
      concept_cache/concept_universe_YYYYMMDD.json -> {code: [theme1, theme2, ...]}

    返回 {code: name} 字典，仅保留属于当日热点题材的股票。
    """
    date_key = date_iso.replace("-", "")
    pattern_path = ROOT / "data" / "pattern_cache" / f"pattern_{date_key}.json"
    concept_path = ROOT / "data" / "concept_cache" / f"concept_universe_{date_key}.json"
    if not pattern_path.exists() or not concept_path.exists():
        return None  # 信号：当天无热点数据，fallback 到全局

    pc = json.loads(pattern_path.read_text(encoding="utf-8"))
    cu = json.loads(concept_path.read_text(encoding="utf-8"))

    hot_raw = pc.get("hot_themes", [])
    hot_set = {t[0] if isinstance(t, list) else t for t in hot_raw}

    pool = {}
    for code, themes in cu.items():
        if code not in universe:
            continue
        if hot_set & set(themes):
            pool[code] = universe[code]
    return pool


def compute_state(g: pd.DataFrame) -> dict:
    if len(g) < 21:
        return {}
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close.iloc[-1])
    h = float(high.iloc[-1])
    l = float(low.iloc[-1])

    ma10 = float(close.rolling(10).mean().iloc[-1])
    avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
    vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0

    ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
    dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0
    close_pos = (cur - l) / (h - l) if (h - l) > 0 else 0.5
    pct_chg = float(pct.iloc[-1]) if len(pct) else 0

    wash, wash_detail = PatternScreener._compute_wash_score(g)

    return {
        "close": round(cur, 2),
        "pct_chg": round(pct_chg, 2),
        "vol_ratio": round(vol_ratio, 2),
        "dist_ma10": round(dist_ma10, 2),
        "ret5": round(ret5, 2),
        "close_pos": round(close_pos, 3),
        "wash": wash,
        "wash_detail": wash_detail,
    }


def check_signal(st_prev: dict, st_cur: dict, require_close_pos: bool = False,
                 require_fund_flow: bool = False,
                 fund_flow_score_min: int = 2) -> bool:
    """检查是否满足 6 条修正规则（可选 close_pos / 资金面 OR 软评分）。

    资金面 3 条满足 fund_flow_score_min 条即通过（OR 逻辑，非 AND）：
      ⑧ T-1 主力净流入 ≥ T1_MAIN_NET_MIN
      ⑨ T 日 5日主力累计 ∈ [T_MAIN5D_MIN, T_MAIN5D_MAX]
      ⑩ T 日小单净流入 ≤ T_SMALL_NET_MAX
    """
    if require_close_pos and st_cur["close_pos"] < CLOSE_POS_MIN:
        return False
    if require_fund_flow:
        score = 0
        if st_prev.get("net_flow") is not None and st_prev["net_flow"] >= T1_MAIN_NET_MIN:
            score += 1
        if st_cur.get("main_5d") is not None and T_MAIN5D_MIN <= st_cur["main_5d"] <= T_MAIN5D_MAX:
            score += 1
        if st_cur.get("smallNet") is not None and st_cur["smallNet"] <= T_SMALL_NET_MAX:
            score += 1
        if score < fund_flow_score_min:
            return False
    return (
        DIST_MA10_T1_MIN <= st_prev["dist_ma10"] <= DIST_MA10_T1_MAX
        and st_prev["wash"] >= WASH_T1_MIN
        and st_prev["ret5"] <= RET5_T1_MAX
        and st_cur["pct_chg"] >= PCT_T_MIN
        and st_cur["vol_ratio"] >= VOL_RATIO_T_MIN
        and (st_cur["dist_ma10"] - st_prev["dist_ma10"]) >= DIST_MA10_DELTA_MIN
    )


def load_fund_flow_index() -> dict:
    """加载 stock_fund_flow 表，按 code 分组，每组是 {date: row_dict}。"""
    db_path = ROOT / "data" / "stock_analysis.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    df = pd.read_sql(
        "SELECT code, date, net_flow, main_net_5d, big_net, big_pct, small_net, small_pct "
        "FROM stock_fund_flow",
        conn,
    )
    conn.close()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    fund_by_code: dict[str, dict[str, dict]] = {}
    for code, group in df.groupby("code"):
        fund_by_code[code] = {row["date"]: row.to_dict() for _, row in group.iterrows()}
    return fund_by_code


def enrich_state_with_fund(state: dict, fund_row: dict | None) -> dict:
    """把资金流字段附加到 state 字典；fund_row 为空则填 None。"""
    if fund_row is None:
        state["net_flow"] = None
        state["main_5d"] = None
        state["big_net"] = None
        state["big_pct"] = None
        state["smallNet"] = None
        state["small_pct"] = None
    else:
        state["net_flow"] = fund_row["net_flow"]
        state["main_5d"] = fund_row["main_net_5d"]
        state["big_net"] = fund_row["big_net"]
        state["big_pct"] = fund_row["big_pct"]
        state["smallNet"] = fund_row["small_net"]
        state["small_pct"] = fund_row["small_pct"]
    return state


def scan_date(df: pd.DataFrame, universe: dict, date_iso: str,
              forward_days: list[int], all_trading_dates: list[str],
              require_close_pos: bool = False,
              require_fund_flow: bool = False,
              fund_flow_score_min: int = 2,
              fund_by_code: dict | None = None,
              allowed_codes: set | None = None) -> list[dict]:
    """扫描单日，返回符合规律的股票列表（含 T+N 涨幅）。

    allowed_codes: 若提供，仅扫描该集合内的股票（用于热点池过滤）。
    fund_by_code: 资金流索引，用于资金面约束。
    """
    future_dates_all = [d for d in all_trading_dates if d > date_iso]
    hits = []
    codes = sorted(universe.keys())
    if allowed_codes is not None:
        codes = [c for c in codes if c in allowed_codes]
    for code in codes:
        g_full = df[df["code"] == code].reset_index(drop=True)
        if len(g_full) < 30:
            continue
        g_dates = g_full["date"].tolist()
        g_t = g_full[g_full["date"] <= date_iso].tail(60).reset_index(drop=True)
        if len(g_t) < 21:
            continue
        g_prev = g_t.iloc[:-1].reset_index(drop=True) if len(g_t) > 1 else g_t
        if len(g_prev) < 21:
            continue

        st_cur = compute_state(g_t)
        st_prev = compute_state(g_prev)
        if not st_cur or not st_prev:
            continue

        # 附加资金流数据
        if fund_by_code is not None:
            code_fund = fund_by_code.get(code, {})
            d_prev = g_prev["date"].iloc[-1] if len(g_prev) else None
            enrich_state_with_fund(st_cur, code_fund.get(date_iso))
            enrich_state_with_fund(st_prev, code_fund.get(d_prev) if d_prev else None)

        if not check_signal(st_prev, st_cur,
                            require_close_pos=require_close_pos,
                            require_fund_flow=require_fund_flow,
                            fund_flow_score_min=fund_flow_score_min):
            continue

        # 计算 T+N 当日涨幅（相对 T+N-1 日 close，非累计从 T 日）
        forward = {}
        prev_close = st_cur["close"]  # T 日 close 作为 T+1 的前一日基准
        for n in forward_days:
            if len(future_dates_all) >= n:
                fd = future_dates_all[n - 1]
                row = g_full[g_full["date"] == fd]
                fc = float(row["close"].iloc[0]) if len(row) else None
                if fc and prev_close and prev_close > 0:
                    forward[f"t+{n}_pct"] = round((fc / prev_close - 1) * 100, 2)
                else:
                    forward[f"t+{n}_pct"] = None
                if fc:
                    prev_close = fc
            else:
                forward[f"t+{n}_pct"] = None

        hits.append({
            "code": code,
            "name": universe[code],
            "date": date_iso,
            "t_pct": st_cur["pct_chg"],
            "t_vol_ratio": st_cur["vol_ratio"],
            "t_close_pos": st_cur["close_pos"],
            "t_dist_ma10": st_cur["dist_ma10"],
            "t1_dist_ma10": st_prev["dist_ma10"],
            "t1_wash": st_prev["wash"],
            "t1_ret5": st_prev["ret5"],
            "dist_ma10_delta": round(st_cur["dist_ma10"] - st_prev["dist_ma10"], 2),
            "t1_wash_detail": st_prev["wash_detail"],
            "t_wash_detail": st_cur["wash_detail"],
            **forward,
        })
    return hits


def _disp_w(s) -> int:
    """显示宽度：中文/全角=2，英文=1。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width: int, align: str = "left") -> str:
    """按显示宽度对齐填充。"""
    s = str(s)
    pad = max(0, width - _disp_w(s))
    if align == "right":
        return " " * pad + s
    if align == "center":
        left = pad // 2
        return " " * left + s + " " * (pad - left)
    return s + " " * pad


def print_table(headers: list[str], rows: list[list], aligns: list[str] | None = None,
                title: str | None = None) -> None:
    """打印 Unicode box drawing 表格（自动适配中文宽度）。"""
    if not headers:
        return
    if aligns is None:
        aligns = ["left"] * len(headers)
    widths = []
    for i, h in enumerate(headers):
        w = _disp_w(h)
        for row in rows:
            if i < len(row) and row[i] is not None:
                w = max(w, _disp_w(row[i]))
        widths.append(w)

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def fmt_row(values: list) -> str:
        cells = [(" " + _pad(v if v is not None else "-", w, a) + " ")
                 for v, w, a in zip(values, widths, aligns)]
        return "│" + "│".join(cells) + "│"

    if title:
        print(f"\n┌─ {title} ─┐")
    else:
        print(border("┌", "┬", "┐"))
    if not title:
        print(fmt_row(headers))
        if rows:
            print(border("├", "┼", "┤"))
    for row in rows:
        print(fmt_row(row))
    if not title:
        print(border("└", "┴", "┘"))


def print_daily_table(hits: list[dict], forward_days: list[int], top_n: int = 50) -> None:
    """单日模式：打印命中股票明细（Unicode box drawing 美化）。"""
    if not hits:
        print("  (无命中)")
        return

    # 列定义：(key, title, align)
    cols: list[tuple[str, str, str]] = [
        ("code",             "代码",     "left"),
        ("name",             "名称",     "left"),
        ("t_pct",            "T涨%",    "right"),
        ("t_vol_ratio",      "量比",     "right"),
        ("t_dist_ma10",      "MA10%",   "right"),
        ("dist_ma10_delta",  "MA10Δ%",  "right"),
        ("t_close_pos",      "收位",     "right"),
        ("t1_wash",          "T-1洗",    "right"),
        ("t1_ret5",          "T-1-5涨%", "right"),
    ]
    for n in forward_days:
        cols.append((f"t+{n}_pct", f"T+{n}%", "right"))
    cols.append(("t1_wash_detail", "T-1 洗盘特征", "left"))
    cols.append(("t_wash_detail",  "T 洗盘特征",  "left"))

    headers = [c[1] for c in cols]
    aligns = [c[2] for c in cols]

    rows = []
    for r in sorted(hits, key=lambda x: -x["t_pct"])[:top_n]:
        row = []
        for key, _, _ in cols:
            v = r.get(key)
            if v is None:
                row.append("-")
            elif key in ("t_pct", "t_vol_ratio", "t_dist_ma10", "dist_ma10_delta",
                          "t_close_pos", "t1_ret5"):
                row.append(f"{v:+.2f}")
            elif key.startswith("t+") and key.endswith("_pct"):
                row.append(f"{v:+.2f}")
            elif key in ("t1_wash",):
                row.append(str(v))
            elif key in ("t1_wash_detail", "t_wash_detail"):
                row.append(str(v)[:20])
            else:
                row.append(str(v))
        rows.append(row)

    print_table(headers, rows, aligns)


def print_backtest_summary(all_hits: list[dict], forward_days: list[int]) -> None:
    """多日模式：打印 T+N 统计（Unicode box drawing 美化）。"""
    # 总体 T+N 统计
    total_dates = len(set(h["date"] for h in all_hits))
    print(f"\n┌──────────────────────────────────────────────┐")
    print(f"│ T+N 回测统计（T+N 当日涨幅，相对 T+N-1 日 close，非累计）")
    print(f"│ 总样本: {len(all_hits)} 只, 覆盖 {total_dates} 个交易日")
    print(f"└──────────────────────────────────────────────┘")

    headers = ["天数", "样本", "均值", "中位", "胜率", "最大", "最小"]
    aligns = ["left", "right", "right", "right", "right", "right", "right"]
    rows = []
    for n in forward_days:
        key = f"t+{n}_pct"
        vals = [h[key] for h in all_hits if h.get(key) is not None]
        if not vals:
            rows.append([f"T+{n}", "0", "-", "-", "-", "-", "-"])
            continue
        s = pd.Series(vals)
        rows.append([
            f"T+{n}",
            str(len(vals)),
            f"{s.mean():+.2f}%",
            f"{s.median():+.2f}%",
            f"{(s > 0).sum() / len(vals) * 100:.1f}%",
            f"{s.max():+.2f}%",
            f"{s.min():+.2f}%",
        ])
    print_table(headers, rows, aligns)

    # 按日分布
    by_date: dict[str, list[dict]] = {}
    for h in all_hits:
        by_date.setdefault(h["date"], []).append(h)

    headers = ["日期", "命中", "均T涨%"] + [f"T+{n}均%" for n in forward_days]
    aligns = ["left", "right", "right"] + ["right"] * len(forward_days)
    rows = []
    for d in sorted(by_date.keys()):
        recs = by_date[d]
        t_pct = pd.Series([r["t_pct"] for r in recs])
        row = [d, str(len(recs)), f"{t_pct.mean():+.2f}%"]
        for n in forward_days:
            vals = [r.get(f"t+{n}_pct") for r in recs if r.get(f"t+{n}_pct") is not None]
            row.append(f"{pd.Series(vals).mean():+.2f}%" if vals else "-")
        rows.append(row)
    print(f"\n┌─ 按日分布 ─┐")
    print_table(headers, rows, aligns)


def main() -> int:
    parser = argparse.ArgumentParser(description="相同规律选股 + T+N 回测")
    parser.add_argument("--date", help=f"单日扫描日期 YYYY-MM-DD（默认 {DEFAULT_SINGLE_DATE}）")
    parser.add_argument("--dates", nargs="+", help="多日扫描（自定义列表）")
    parser.add_argument("--backtest", action="store_true",
                        help=f"使用预置 {len(DEFAULT_BACKTEST_DATES)} 天窗口回测")
    parser.add_argument("--window-days", type=int,
                        help="取预置窗口最后 N 天（如 --window-days 7）")
    parser.add_argument("--require-close-pos", action="store_true",
                        help=f"启用第 7 条强约束：T 日 close_pos ≥ {CLOSE_POS_MIN}")
    parser.add_argument("--hot-only", action="store_true",
                        help="仅扫描当日热点题材池（从 pattern_cache/concept_cache 构建）")
    parser.add_argument("--require-fund-flow", action="store_true",
                        help="启用资金面 OR 软评分（3 条满足 N 条即通过，N 由 --fund-flow-score-min 控制）")
    parser.add_argument("--fund-flow-score-min", type=int, default=2,
                        help="资金面 OR 软评分阈值（默认 2：3 条至少满足 2 条）")
    parser.add_argument("--forward-days", type=int, nargs="+", default=DEFAULT_FORWARD_DAYS,
                        help="T+N 天数（默认 1~10）")
    parser.add_argument("--end-date", default="2026-06-22",
                        help="数据截止日期（默认 2026-06-22）")
    args = parser.parse_args()

    # 决定扫描日期列表
    if args.backtest:
        scan_dates = DEFAULT_BACKTEST_DATES
        if args.window_days:
            scan_dates = scan_dates[-args.window_days:]
    elif args.dates:
        scan_dates = args.dates
    else:
        scan_dates = [args.date or DEFAULT_SINGLE_DATE]

    universe = load_universe()
    db = DatabaseManager()
    df_all = db.get_bulk_daily_data(days=120, end_date=args.end_date)
    df_all["date"] = pd.to_datetime(df_all["date"]).dt.strftime("%Y-%m-%d")
    df = df_all[df_all["code"].isin(universe.keys())].copy()
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    all_trading_dates = sorted(df["date"].unique().tolist())

    print("=" * 80)
    print("相同规律选股（6 条修正规则）")
    print("=" * 80)
    print(f"规则  T-1: dist_ma10∈[{DIST_MA10_T1_MIN},{DIST_MA10_T1_MAX}] "
          f"+ wash≥{WASH_T1_MIN} + ret5≤{RET5_T1_MAX}")
    print(f"      T  : pct≥{PCT_T_MIN} + vol_ratio≥{VOL_RATIO_T_MIN} "
          f"+ dist_ma10Δ≥{DIST_MA10_DELTA_MIN}"
          f"{' + close_pos≥'+str(CLOSE_POS_MIN) if args.require_close_pos else ''}")
    print(f"扫描日期 ({len(scan_dates)} 天): {scan_dates[0]} ~ {scan_dates[-1]}"
          if len(scan_dates) > 1 else f"扫描日期: {scan_dates[0]}")
    print(f"T+N: {args.forward_days}")
    print(f"候选池: {len(universe)} 只, 加载 {len(df)} 行, "
          f"{df['code'].nunique()} 只股票, {len(all_trading_dates)} 个交易日"
          f"{', 模式=热点池限定' if args.hot_only else ''}"
          f"{', 资金面 3 条约束' if args.require_fund_flow else ''}")
    print()

    # 加载资金流索引（按需）
    fund_by_code = load_fund_flow_index() if args.require_fund_flow else None
    if args.require_fund_flow:
        print(f"资金流索引加载: {len(fund_by_code)} 只股票")
        print()

    all_hits = []
    for d in scan_dates:
        if d not in all_trading_dates:
            print(f"[跳过] {d} 非交易日或无数据")
            continue
        # 热点池过滤
        allowed_codes = None
        if args.hot_only:
            pool = load_hot_pool(d, universe)
            if pool is None:
                print(f"[{d}] 热点数据缺失，fallback 到全市场")
            else:
                allowed_codes = set(pool.keys())
        t0 = time.time()
        hits = scan_date(df, universe, d, args.forward_days, all_trading_dates,
                         require_close_pos=args.require_close_pos,
                         require_fund_flow=args.require_fund_flow,
                         fund_flow_score_min=args.fund_flow_score_min,
                         fund_by_code=fund_by_code,
                         allowed_codes=allowed_codes)
        all_hits.extend(hits)
        pool_size = len(allowed_codes) if allowed_codes else len(universe)
        print(f"[{d}] 池={pool_size:>4} → 命中 {len(hits):>3} 只  (耗时 {time.time()-t0:.0f}s)")

    # 输出
    if len(scan_dates) == 1:
        print(f"\n=== {scan_dates[0]} 符合 6 条规律的股票（共 {len(all_hits)} 只）===")
        print_daily_table(all_hits, args.forward_days)
    else:
        print_backtest_summary(all_hits, args.forward_days)

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "same_pattern_hits.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "rules": {
                "DIST_MA10_T1": [DIST_MA10_T1_MIN, DIST_MA10_T1_MAX],
                "WASH_T1_MIN": WASH_T1_MIN, "RET5_T1_MAX": RET5_T1_MAX,
                "PCT_T_MIN": PCT_T_MIN, "VOL_RATIO_T_MIN": VOL_RATIO_T_MIN,
                "DIST_MA10_DELTA_MIN": DIST_MA10_DELTA_MIN,
            },
            "scan_dates": scan_dates,
            "forward_days": args.forward_days,
            "total_hits": len(all_hits),
            "hits": all_hits,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
