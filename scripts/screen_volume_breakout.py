# -*- coding: utf-8 -*-
"""放量启动型选股策略 —— 基于 06-22 推送 10 只股票共性规律提取。

7 条信号规则：
  T-1 日（信号前一交易日）：
    ① dist_ma10 ∈ [-5%, +5%]              贴近 MA10
    ② wash_score > 0                       已出现洗盘特征
    ③ ret5 ≤ +15%                          未明显拉升
  T 日（信号当日）：
    ④ pct_chg ≥ +3%                        放量上涨
    ⑤ vol_ratio Δ ≥ +0.2                   量能放大
    ⑥ close_pos > 0.8                      收高位
    ⑦ dist_ma10 Δ ≥ +2%                    加速向上

对照组 A：满足 T-1 ①②③ 但不满足 T ④⑤⑥⑦（横盘未启动）
对照组 B：满足 T ④⑤⑥⑦ 但不满足 T-1 ①②③（无洗盘基础直接涨）

输出：
  - 最近 20 交易日信号命中明细
  - T+1 / T+3 / T+5 平均涨幅对比（信号组 vs 对照组）
  - 最近一次信号股 TOP 30
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.pattern_screener import PatternScreener
from src.storage import DatabaseManager

# ---- 信号参数 ----
DIST_MA10_T1_MIN, DIST_MA10_T1_MAX = -5.0, 5.0       # ①
WASH_T1_MIN = 1                                       # ② 至少 1 分洗盘特征
RET5_T1_MAX = 15.0                                    # ③
PCT_T_MIN = 3.0                                       # ④
VOL_RATIO_DELTA_MIN = 0.2                             # ⑤
CLOSE_POS_MIN = 0.8                                   # ⑥ 收高位
DIST_MA10_DELTA_MIN = 2.0                             # ⑦

# ---- 回测参数 ----
WINDOW_DATES = [
    "2026-05-22", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28",
    "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
    "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11",
    "2026-06-12", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
    "2026-06-19", "2026-06-22",
]
FORWARD_DAYS = [1, 3, 5]
LAST_SIGNAL_DATE = "2026-06-19"  # 06-22 是最后一天，没有 T+1，所以"最近信号"看 06-19


def is_excluded_board(code: str) -> bool:
    c = code.strip()
    if c.startswith("30"): return True      # 创业板
    if c.startswith("688") or c.startswith("689"): return True  # 科创板
    if c.startswith("8") or c.startswith("4") or c.startswith("920"): return True  # 北交所
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


def compute_state(g: pd.DataFrame) -> dict:
    """计算 g 最后一日的状态指标。"""
    if len(g) < 21:
        return {}
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    open_ = g["open"].astype(float)
    pct = g["pct_chg"].astype(float)
    volume = g["volume"].astype(float)
    cur = float(close.iloc[-1])
    h = float(high.iloc[-1])
    l = float(low.iloc[-1])
    o = float(open_.iloc[-1])

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


def check_signal(st_prev: dict, st_cur: dict) -> tuple[bool, dict]:
    """检查是否满足 7 条信号规则，返回命中详情。"""
    t1_ok = (
        DIST_MA10_T1_MIN <= st_prev["dist_ma10"] <= DIST_MA10_T1_MAX
        and st_prev["wash"] >= WASH_T1_MIN
        and st_prev["ret5"] <= RET5_T1_MAX
    )
    t_ok = (
        st_cur["pct_chg"] >= PCT_T_MIN
        and (st_cur["vol_ratio"] - st_prev["vol_ratio"]) >= VOL_RATIO_DELTA_MIN
        and st_cur["close_pos"] >= CLOSE_POS_MIN
        and (st_cur["dist_ma10"] - st_prev["dist_ma10"]) >= DIST_MA10_DELTA_MIN
    )
    # 对照组 A：T-1 OK，T 不 OK
    # 对照组 B：T OK，T-1 不 OK
    full_signal = t1_ok and t_ok
    ctrl_a = t1_ok and not t_ok
    ctrl_b = t_ok and not t1_ok
    cat = "signal" if full_signal else ("ctrl_a" if ctrl_a else ("ctrl_b" if ctrl_b else "none"))

    return cat, {
        "t1_dist_ma10": st_prev["dist_ma10"], "t1_wash": st_prev["wash"],
        "t1_ret5": st_prev["ret5"], "t1_wash_detail": st_prev["wash_detail"],
        "t_pct": st_cur["pct_chg"], "t_vol_ratio": st_cur["vol_ratio"],
        "t_close_pos": st_cur["close_pos"], "t_dist_ma10": st_cur["dist_ma10"],
        "t_wash_detail": st_cur["wash_detail"],
        "vol_ratio_delta": round(st_cur["vol_ratio"] - st_prev["vol_ratio"], 2),
        "dist_ma10_delta": round(st_cur["dist_ma10"] - st_prev["dist_ma10"], 2),
    }


def main() -> int:
    universe = load_universe()
    print(f"候选池（全 A 股）: {len(universe)} 只")
    print(f"窗口: {WINDOW_DATES[0]} ~ {WINDOW_DATES[-1]}")
    print(f"T+1/T+3/T+5 后续涨幅评估")
    print()

    db = DatabaseManager()
    end_date = WINDOW_DATES[-1]
    df_all = db.get_bulk_daily_data(days=90, end_date=end_date)  # end_date=06-22，向前 90 日
    df = df_all[df_all["code"].isin(universe.keys())].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"加载 {len(df_all)} 行 → 过滤后 {len(df)} 行, {df['code'].nunique()} 只")

    # 构建每只股票的日期 -> close 映射，便于算 T+N
    code_date_close = {}
    for code in universe:
        g = df[df["code"] == code].reset_index(drop=True)
        code_date_close[code] = {row["date"]: row["close"] for _, row in g.iterrows()}

    signal_records = []     # 信号命中
    ctrl_a_records = []     # 对照 A
    ctrl_b_records = []     # 对照 B

    t0 = time.time()
    codes = sorted(universe.keys())
    for i, code in enumerate(codes):
        if (i + 1) % 1000 == 0:
            print(f"  进度 {i+1}/{len(codes)}, 已用 {time.time()-t0:.0f}s, "
                  f"信号 {len(signal_records)} / A {len(ctrl_a_records)} / B {len(ctrl_b_records)}")

        g_full = df[df["code"] == code].reset_index(drop=True)
        if len(g_full) < 30:
            continue
        name = universe[code]
        date_close = code_date_close[code]

        for idx, d in enumerate(WINDOW_DATES):
            # T 日数据
            g_t = g_full[g_full["date"] <= d].tail(60).reset_index(drop=True)
            if len(g_t) < 21:
                continue

            # T-1 日（前一交易日）数据
            g_t_prev = g_t.iloc[:-1].reset_index(drop=True) if len(g_t) > 1 else g_t
            if len(g_t_prev) < 21:
                continue

            st_cur = compute_state(g_t)
            st_prev = compute_state(g_t_prev)
            if not st_cur or not st_prev:
                continue

            cat, metrics = check_signal(st_prev, st_cur)
            if cat == "none":
                continue

            # 算 T+N 涨幅
            # 找 d 之后第 N 个交易日的 close
            close_t = st_cur["close"]
            future_dates = [dd for dd in WINDOW_DATES if dd > d]
            forward = {}
            for n in FORWARD_DAYS:
                if len(future_dates) >= n:
                    fd = future_dates[n - 1]
                    fc = date_close.get(fd)
                    if fc and close_t > 0:
                        forward[f"t+{n}_pct"] = round((fc / close_t - 1) * 100, 2)
                    else:
                        forward[f"t+{n}_pct"] = None
                else:
                    forward[f"t+{n}_pct"] = None

            rec = {"code": code, "name": name, "date": d, **metrics, **forward}
            if cat == "signal":
                signal_records.append(rec)
            elif cat == "ctrl_a":
                ctrl_a_records.append(rec)
            elif cat == "ctrl_b":
                ctrl_b_records.append(rec)

    print(f"\n扫描完成，耗时 {time.time()-t0:.0f}s")
    print(f"  信号组 (T-1 ✓ + T ✓): {len(signal_records)}")
    print(f"  对照 A (T-1 ✓ + T ✗): {len(ctrl_a_records)}  -- 洗盘但未启动")
    print(f"  对照 B (T-1 ✗ + T ✓): {len(ctrl_b_records)}  -- 无洗盘基础直接涨")

    # === 后续涨幅统计 ===
    def stat_forward(records: list, label: str) -> dict:
        out = {}
        for n in FORWARD_DAYS:
            key = f"t+{n}_pct"
            vals = [r[key] for r in records if r.get(key) is not None]
            if not vals:
                out[key] = None
                continue
            s = pd.Series(vals)
            out[key] = {
                "count": len(vals),
                "mean": round(s.mean(), 2),
                "median": round(s.median(), 2),
                "win_rate": round((s > 0).sum() / len(vals) * 100, 1),
                "max": round(s.max(), 2),
                "min": round(s.min(), 2),
            }
        return out

    print("\n=== 后续涨幅统计（T 日收盘买入，基准 = T 日 close）===")
    print(f"{'组别':<12}{'样本':>8}{'T+1 均':>10}{'T+1 胜':>8}{'T+3 均':>10}{'T+3 胜':>8}{'T+5 均':>10}{'T+5 胜':>8}")
    print("-" * 80)
    for label, recs in [("信号组", signal_records), ("对照 A", ctrl_a_records), ("对照 B", ctrl_b_records)]:
        st = stat_forward(recs, label)
        row = f"{label:<12}"
        cnt = st.get("t+1_pct", {}).get("count", 0) if st.get("t+1_pct") else 0
        row += f"{cnt:>8}"
        for n in FORWARD_DAYS:
            v = st.get(f"t+{n}_pct")
            if v:
                row += f"{v['mean']:>+10.2f}{v['win_rate']:>+7.1f}%"
            else:
                row += " " * 18
        print(row)

    # === 最近一次信号股 TOP N ===
    print(f"\n=== 最近一次信号股（{LAST_SIGNAL_DATE} 触发）===")
    latest = [r for r in signal_records if r["date"] == LAST_SIGNAL_DATE]
    latest.sort(key=lambda x: -x["t_pct"])
    print(f"{'代码':<7}{'名称':<10}{'信号日':<12}{'T涨':>7}{'量比Δ':>7}{'MA10Δ':>7}"
          f"{'收位':>6}{'T+1':>8}{'T+3':>8}{'T+5':>8}{'T-1 洗盘特征':<24}{'T 洗盘特征':<24}")
    print("-" * 140)
    for r in latest[:30]:
        print(f"{r['code']:<7}{r['name'][:8]:<10}{r['date']:<12}{r['t_pct']:>+7.2f}"
              f"{r['vol_ratio_delta']:>+7.2f}{r['dist_ma10_delta']:>+7.2f}{r['t_close_pos']:>6.2f}"
              f"{str(r.get('t+1_pct','')):>8}{str(r.get('t+3_pct','')):>8}{str(r.get('t+5_pct','')):>8}"
              f"{r['t1_wash_detail'][:22]:<24}{r['t_wash_detail'][:22]:<24}")

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "volume_breakout_signals.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "params": {
                "DIST_MA10_T1": [DIST_MA10_T1_MIN, DIST_MA10_T1_MAX],
                "WASH_T1_MIN": WASH_T1_MIN, "RET5_T1_MAX": RET5_T1_MAX,
                "PCT_T_MIN": PCT_T_MIN, "VOL_RATIO_DELTA_MIN": VOL_RATIO_DELTA_MIN,
                "CLOSE_POS_MIN": CLOSE_POS_MIN, "DIST_MA10_DELTA_MIN": DIST_MA10_DELTA_MIN,
            },
            "summary": {
                "signal_count": len(signal_records),
                "ctrl_a_count": len(ctrl_a_records),
                "ctrl_b_count": len(ctrl_b_records),
                "signal_stats": stat_forward(signal_records, "signal"),
                "ctrl_a_stats": stat_forward(ctrl_a_records, "ctrl_a"),
                "ctrl_b_stats": stat_forward(ctrl_b_records, "ctrl_b"),
            },
            "signals": signal_records,
            "ctrl_a": ctrl_a_records[:200],  # 只存前 200 避免文件过大
            "ctrl_b": ctrl_b_records[:200],
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
