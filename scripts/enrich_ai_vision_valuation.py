# -*- coding: utf-8 -*-
"""给 AI 视觉洗盘 TOP 候选补充 PE/PB/市值，筛龙头股。"""
from __future__ import annotations

import json
import re
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
FIELD_PATTERNS = {
    "pe_dynamic": r'id="dtsyl"[^>]*>([^<]+)</span>',
    "pe_static": r'id="jtsyl"[^>]*>([^<]+)</span>',
    "pb": r'id="sjl"[^>]*>([^<]+)</span>',
    "total_mv_str": r'id="stockzsz"[^>]*>([^<]+)</span>',
    "rev_str": r"营业总收入[^<]*</span>\s*<span[^>]*>([^<]+)</span>",
}


def parse_num(s):
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


def fetch_one(code):
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


def _disp_w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, w, a="left"):
    s = str(s)
    p = max(0, w - _disp_w(s))
    return " " * p + s if a == "right" else s + " " * p


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
    scan_json = ROOT / "data" / "_tmp_theme_filter" / "ai_vision_wash_scan.json"
    data = json.loads(scan_json.read_text(encoding="utf-8"))
    recs = sorted(data["records"], key=lambda x: -x["wash"])

    name_df = pd.read_csv(ROOT / "data" / "_tmp_theme_filter" / "a_code_name.csv",
                          dtype={"code": str})
    name_df["code"] = name_df["code"].str.zfill(6)
    name_map = dict(zip(name_df["code"], name_df["name"]))

    enriched = []
    for i, r in enumerate(recs[:30]):  # TOP 30
        code = r["code"]
        v = fetch_one(code)
        enriched.append({
            **r,
            "name": name_map.get(code, ""),
            "pe": v.get("pe_dynamic"),
            "pe_static": v.get("pe_static"),
            "pb": v.get("pb"),
            "total_mv": v.get("total_mv_str"),
            "revenue": v.get("rev_str"),
        })
        if (i + 1) % 5 == 0:
            print(f"  进度 {i+1}/30")
        time.sleep(0.5)

    # 保存
    out = ROOT / "data" / "_tmp_theme_filter" / "ai_vision_with_valuation.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2, default=str)

    # 按市值降序输出（识别龙头）
    enriched.sort(key=lambda x: -(x.get("total_mv") or 0))

    headers = ["代码", "名称", "题材", "Wash", "T涨%", "MA10%", "PE动", "PB", "市值亿", "营收亿"]
    aligns = ["left", "left", "left", "right", "right", "right", "right", "right", "right", "right"]
    rows = []
    for r in enriched:
        rows.append([
            r["code"],
            str(r["name"])[:8],
            ",".join(r["concepts"])[:12],
            r["wash"],
            f"{r['pct_chg']:+.2f}",
            f"{r['dist_ma10']:+.2f}",
            f"{r['pe']:.1f}" if r.get("pe") else "-",
            f"{r['pb']:.2f}" if r.get("pb") else "-",
            f"{r['total_mv']/1e8:.0f}" if r.get("total_mv") else "-",
            f"{r['revenue']/1e8:.1f}" if r.get("revenue") else "-",
        ])
    print(f"\n=== AI 视觉洗盘 TOP 30（按市值降序）===")
    table(headers, rows, aligns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
