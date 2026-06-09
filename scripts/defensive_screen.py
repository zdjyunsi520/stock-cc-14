# -*- coding: utf-8 -*-
"""一次性脚本：筛选防御性/超跌反弹股票。"""

import sqlite3
import numpy as np
import pandas as pd


def main():
    conn = sqlite3.connect("data/stock_analysis.db")
    sql = """SELECT code, date, open, high, low, close, volume, amount, pct_chg
             FROM stock_daily WHERE date >= '2026-05-20' ORDER BY code, date"""
    df = pd.read_sql(sql, conn)
    conn.close()

    print(f"总数据: {len(df)} 行, 股票数: {df['code'].nunique()}")

    rows = []
    for code, g in df.groupby("code"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < 10:
            continue

        close = g["close"].astype(float)
        pct = g["pct_chg"].astype(float)

        volatility = float(pct.std())

        cummax = close.cummax()
        drawdown = float(((close - cummax) / cummax).min()) * 100

        total_return = float((close.iloc[-1] / close.iloc[0] - 1)) * 100
        up_ratio = float((pct > 0).sum() / len(pct)) * 100

        cur = float(close.iloc[-1])

        ma5 = float(close.rolling(5).mean().iloc[-1])
        ma10 = float(close.rolling(10).mean().iloc[-1])
        ma20_s = close.rolling(20).mean()
        ma20 = float(ma20_s.iloc[-1]) if not pd.isna(ma20_s.iloc[-1]) else 0.0

        ma_bull = cur >= ma5 >= ma10 >= ma20 if ma20 > 0 else False

        score = 0.0
        score += max(0, 50 - volatility * 10)
        score += max(0, 30 + drawdown)
        score += up_ratio * 0.2
        if ma_bull:
            score += 10

        rows.append((
            code, cur, volatility, drawdown, total_return, up_ratio, ma_bull, score
        ))

    rdf = pd.DataFrame(rows, columns=[
        "code", "close", "vol", "dd", "ret", "up", "mabull", "score",
    ])
    print(f"有效分析: {len(rdf)} 只\n")

    # === 1. 防御性股票 ===
    defensive = rdf[(rdf["vol"] < 2.5) & (rdf["close"] >= 5) & (rdf["dd"] > -10)]
    defensive = defensive.sort_values("score", ascending=False)

    print("=" * 80)
    print("  防御性股票 TOP30（低波动 + 小回撤 + 均线多头加分）")
    print("=" * 80)
    for _, r in defensive.head(30).iterrows():
        ma_tag = "MA多头" if r["mabull"] else "      "
        print(
            f"  {r['code']}  收盘{r['close']:>7.2f}  "
            f"波动{r['vol']:>5.2f}%  回撤{r['dd']:>+6.2f}%  "
            f"涨跌{r['ret']:>+6.2f}%  上涨率{r['up']:>5.1f}%  "
            f"{ma_tag}  评分{r['score']:>5.1f}"
        )

    # === 2. 超跌反弹 ===
    oversold = rdf[(rdf["dd"] < -8) & (rdf["close"] >= 3) & (rdf["ret"] > -5)]
    oversold = oversold.sort_values("score", ascending=False)

    print(f"\n{'=' * 80}")
    print("  超跌反弹候选（30日内回撤>8%但近期企稳反弹）")
    print("=" * 80)
    for _, r in oversold.head(20).iterrows():
        ma_tag = "MA多头" if r["mabull"] else "      "
        print(
            f"  {r['code']}  收盘{r['close']:>7.2f}  "
            f"波动{r['vol']:>5.2f}%  回撤{r['dd']:>+6.2f}%  "
            f"涨跌{r['ret']:>+6.2f}%  上涨率{r['up']:>5.1f}%  "
            f"{ma_tag}  评分{r['score']:>5.1f}"
        )

    # === 3. 低价大盘防御 ===
    big_def = rdf[(rdf["close"] < 10) & (rdf["close"] >= 3) & (rdf["vol"] < 2.0)]
    big_def = big_def.sort_values("vol")

    print(f"\n{'=' * 80}")
    print("  低价大盘防御（<10元，波动率最低，类似银行/公用事业特征）")
    print("=" * 80)
    for _, r in big_def.head(20).iterrows():
        print(
            f"  {r['code']}  收盘{r['close']:>7.2f}  "
            f"波动{r['vol']:>5.2f}%  回撤{r['dd']:>+6.2f}%  "
            f"涨跌{r['ret']:>+6.2f}%"
        )


if __name__ == "__main__":
    main()
