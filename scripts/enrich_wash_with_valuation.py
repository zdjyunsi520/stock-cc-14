# -*- coding: utf-8 -*-
"""从 themes_wash_scan.json 选出洗盘 TOP 候选，补充 PE/PB/市值/营收。

数据源：同花顺 basic.10jqka.com.cn/{code}/ 页面 HTML 解析
字段 id：
  dtsyl   动态市盈率 PE
  jtsyl   静态市盈率 PE
  sjl     市净率 PB
  stockzsz 总市值
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
    "Referer": "https://www.10jqka.com.cn/",
}

# 同花顺 PE/PB 字段 id（页面 HTML 里直接含值）
FIELD_PATTERNS = {
    "pe_dynamic": r'id="dtsyl"[^>]*>([^<]+)</span>',
    "pe_static": r'id="jtsyl"[^>]*>([^<]+)</span>',
    "pb": r'id="sjl"[^>]*>([^<]+)</span>',
    "total_mv_str": r'id="stockzsz"[^>]*>([^<]+)</span>',
    "rev_str": r"营业总收入[^<]*</span>\s*<span[^>]*>([^<]+)</span>",
    "net_profit_str": r"净利润[^<]*</span>\s*<span[^>]*>([^<]+)</span>",
    "roe_str": r"id=\"roe\"[^>]*>([^<]+)</span>",
}


def parse_num(s: str) -> float | None:
    """解析 '15280亿' / '14.02' / '5.65%' 等字符串为数值。"""
    if not s:
        return None
    s = s.strip().replace(",", "").replace("%", "")
    m = re.match(r"-?[\d.]+", s)
    if not m:
        return None
    try:
        val = float(m.group(0))
    except ValueError:
        return None
    if "亿" in s:
        val *= 1e8
    elif "万" in s:
        val *= 1e4
    return val


def fetch_one(code: str) -> dict:
    """从同花顺 basic 页面拉一只股票的估值字段。"""
    url = f"https://basic.10jqka.com.cn/{code}/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "gbk"
        if r.status_code != 200:
            return {}
        out = {"code": code}
        for key, pat in FIELD_PATTERNS.items():
            m = re.search(pat, r.text)
            if m:
                v = parse_num(m.group(1))
                if v is not None:
                    out[key] = v
        return out
    except Exception as e:
        print(f"  [warn] {code}: {e}")
        return {}


def pick_candidates(scan_json: Path, top_per_theme: int = 15) -> list[dict]:
    """从洗盘扫描结果里挑 TOP 候选。"""
    data = json.loads(scan_json.read_text(encoding="utf-8"))
    candidates = []
    for theme, recs in data["records"].items():
        # 过滤：横盘洗盘形态（|pct_chg|<=5 + vol_ratio<1.5），按 wash 降序
        filtered = [r for r in recs
                    if abs(r["pct_chg"]) <= 5 and r["vol_ratio"] < 1.5 and r["wash"] >= 4]
        filtered.sort(key=lambda x: -x["wash"])
        for r in filtered[:top_per_theme]:
            r = {**r, "theme": theme}
            candidates.append(r)
    return candidates


def _disp_w(s) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, w, align="left"):
    s = str(s)
    p = max(0, w - _disp_w(s))
    return " " * p + s if align == "right" else s + " " * p


def table(headers, rows, aligns):
    widths = []
    for i, h in enumerate(headers):
        w = _disp_w(h)
        for r in rows:
            if i < len(r) and r[i] is not None:
                w = max(w, _disp_w(r[i]))
        widths.append(w)
    b = lambda l, m, r: l + m.join("─" * (w + 2) for w in widths) + r
    fr = lambda vs: "│" + "│".join(" " + _pad(v if v is not None else "-", w, a) + " "
                                    for v, w, a in zip(vs, widths, aligns)) + "│"
    print(b("┌", "┬", "┐"))
    print(fr(headers))
    if rows:
        print(b("├", "┼", "┤"))
    for r in rows:
        print(fr(r))
    print(b("└", "┴", "┘"))


def main() -> int:
    scan_json = ROOT / "data" / "_tmp_theme_filter" / "themes_wash_scan.json"
    candidates = pick_candidates(scan_json, top_per_theme=15)
    print(f"挑出候选: {len(candidates)} 只")

    # 股票名
    name_df = pd.read_csv(ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv",
                          dtype={"code": str})
    name_df["code"] = name_df["code"].str.zfill(6)
    name_map = dict(zip(name_df["code"], name_df["name"]))

    enriched = []
    for i, c in enumerate(candidates):
        code = c["code"]
        v = fetch_one(code)
        # 合并
        rec = {
            "code": code,
            "name": name_map.get(code, ""),
            "theme": c["theme"],
            "wash": c["wash"],
            "t_pct": c["pct_chg"],
            "vol_ratio": c["vol_ratio"],
            "dist_ma10": c["dist_ma10"],
            "ret5": c["ret5"],
            "wash_detail": c["wash_detail"],
            "pe": v.get("pe_dynamic"),
            "pe_static": v.get("pe_static"),
            "pb": v.get("pb"),
            "total_mv": v.get("total_mv_str"),
            "revenue": v.get("rev_str"),
            "net_profit": v.get("net_profit_str"),
            "roe": v.get("roe_str"),
        }
        enriched.append(rec)
        if (i + 1) % 5 == 0:
            print(f"  进度 {i+1}/{len(candidates)}")
        time.sleep(0.5)  # 0.5s 间隔 ≈ 2 req/s，40 只 ≈ 20s

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "wash_with_valuation.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果: {out}")

    # 输出表格：PE/PB/市值
    headers = ["代码", "名称", "题材", "Wash", "PE动", "PE静", "PB", "市值亿", "营收亿", "净利亿", "ROE%"]
    aligns = ["left"] * 3 + ["right"] * 8
    rows = []
    for r in enriched:
        rows.append([
            r["code"],
            str(r["name"])[:8],
            r["theme"][:6],
            r["wash"],
            f"{r['pe']:.1f}" if r["pe"] else "-",
            f"{r['pe_static']:.1f}" if r["pe_static"] else "-",
            f"{r['pb']:.2f}" if r["pb"] else "-",
            f"{r['total_mv']/1e8:.0f}" if r["total_mv"] else "-",
            f"{r['revenue']/1e8:.1f}" if r["revenue"] else "-",
            f"{r['net_profit']/1e8:.2f}" if r["net_profit"] else "-",
            f"{r['roe']:.2f}" if r["roe"] else "-",
        ])
    table(headers, rows, aligns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
