# -*- coding: utf-8 -*-
"""抓取个股 1 分钟分时数据，存入 stock_1min_kline 表。

数据源：同花顺 d.10jqka.com.cn/v6/line/hs_{code}/60/all.js
单次返回约 9881 条（41 个交易日）。返回 dates + price + volumn 数组格式。

用法:
    python -m scripts.scrape_1min_kline 600760           # 单只
    python -m scripts.scrape_1min_kline --max-stocks 50  # 限制数量
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage import DatabaseManager, Stock1minKline

logger = logging.getLogger(__name__)

URL_ALL = "https://d.10jqka.com.cn/v6/line/hs_{code}/60/all.js"
URL_LAST = "https://d.10jqka.com.cn/v6/line/hs_{code}/60/last.js"
# 兼容旧引用
URL = URL_ALL

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _curl(url: str, code: str, timeout: int = 15) -> str:
    """统一的 curl GET 封装，返回 stdout 文本。失败返回空串。"""
    cmd = [
        "curl", "-s", "-m", str(timeout),
        "-A", UA,
        "-H", "Referer: https://stockpage.10jqka.com.cn/",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except Exception as e:
        logger.warning("fetch %s curl 失败: %s", code, e)
        return ""
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    if not stdout or len(stdout) < 200:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:200] if proc.stderr else ""
        logger.warning("fetch %s 空响应 stderr=%s", code, stderr)
        return ""
    return stdout


def fetch_1min_kline(code: str) -> List[dict]:
    """抓取单只股票的 1 分钟分时数据（合并 all.js + last.js）。

    数据源：
    - all.js: 41 天全量，但当天数据有 T+1 延迟，最新只到 ~11:24
    - last.js: 最新 140 条（≈2.3h），覆盖最近到 15:00 收盘
    - 合并后可拿到当天完整 9:30-15:00 数据

    Args:
        code: 6 位股票代码

    Returns:
        list[dict]，字段 code/ts/price/volume/amount，按 ts 升序。
    """
    code = str(code).zfill(6)

    # 1. all.js（41 天历史 + 当天上午）
    all_text = _curl(URL_ALL.format(code=code), code)
    rows_all = _parse_all_js(all_text, code) if all_text else []

    # 2. last.js（最近 140 条，含当天下午）
    last_text = _curl(URL_LAST.format(code=code), code)
    rows_last = _parse_last_js(last_text, code) if last_text else []

    # 3. 按 ts 合并去重（last 优先，覆盖 all）
    by_ts: dict = {}
    for r in rows_all:
        by_ts[r["ts"]] = r
    for r in rows_last:
        by_ts[r["ts"]] = r

    return sorted(by_ts.values(), key=lambda r: r["ts"])


def _parse_all_js(text: str, code: str) -> List[dict]:
    """解析 all.js JSONP 响应为 1min K dict 列表。

    响应结构：quotebridge_v6_line_hs_{code}_60_all({...})
    JSON 字段：total/start/dates/price/volumn 等
    """
    # 提取 JSON
    m = re.search(r"quotebridge_v6_line_hs_\d+_60_all\((.+)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        logger.warning("parse %s: 未匹配 JSONP 包装", code)
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("parse %s JSON 失败: %s", code, e)
        return []

    dates_arr = (data.get("dates") or "").split(",")
    price_arr = (data.get("price") or "").split(",")
    vol_arr = (data.get("volumn") or "").split(",")
    start_str = data.get("start") or ""

    if not dates_arr or not price_arr or not start_str:
        logger.warning("parse %s: 字段缺失", code)
        return []

    try:
        start_year = int(start_str[:4])
    except ValueError:
        logger.warning("parse %s: start 格式异常 start=%s", code, start_str)
        return []

    out: List[dict] = []
    cur_year = start_year
    last_mmdd = None

    for i, mmddhhmm in enumerate(dates_arr):
        if len(mmddhhmm) != 8:
            continue
        try:
            mm = int(mmddhhmm[:2])
            dd = int(mmddhhmm[2:4])
            hh = int(mmddhhmm[4:6])
            minute = int(mmddhhmm[6:8])
        except ValueError:
            continue

        # 跨年检测：MMDD 变小（12-31 → 01-02）
        cur_mmdd = mm * 100 + dd
        if last_mmdd is not None and cur_mmdd < last_mmdd:
            cur_year += 1
        last_mmdd = cur_mmdd

        try:
            ts = datetime(cur_year, mm, dd, hh, minute)
        except ValueError:
            continue

        idx = i * 4
        if idx >= len(price_arr):
            break
        try:
            price = int(price_arr[idx]) / 100.0  # 元
        except (ValueError, IndexError):
            continue

        if i < len(vol_arr) and vol_arr[i]:
            try:
                volume = int(vol_arr[i])
            except ValueError:
                volume = 0
        else:
            volume = 0

        out.append({
            "code": str(code).zfill(6),
            "ts": ts,
            "price": price,
            "volume": volume,
            "amount": price * volume,
        })

    return out


def _parse_last_js(text: str, code: str) -> List[dict]:
    """解析 last.js 响应（OHLC CSV 格式，最新 140 条）。

    响应结构：quotebridge_v6_line_hs_{code}_60_last({...
        "data":"yyyyMMddHHmm,O,H,L,C,V,A,...;yyyyMMddHHmm,..."
    })
    字段索引: 0=ts, 1=open, 2=high, 3=low, 4=close, 5=volume, 6=amount

    Returns:
        list[dict]，字段 code/ts/price/volume/amount（price 取 close）。
    """
    m = re.search(r"quotebridge_v6_line_hs_\d+_60_last\((.+)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        logger.warning("parse_last %s: 未匹配 JSONP 包装", code)
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("parse_last %s JSON 失败: %s", code, e)
        return []

    data_str = data.get("data") or ""
    if not data_str:
        return []

    out: List[dict] = []
    for row in data_str.split(";"):
        cells = row.split(",")
        if len(cells) < 7:
            continue
        try:
            ts_str = cells[0]
            ts = datetime.strptime(ts_str, "%Y%m%d%H%M")
            price = float(cells[4])      # close
            volume = int(cells[5])       # 成交量
            amount = float(cells[6])     # 成交额（已直接是元）
        except (ValueError, IndexError):
            continue
        out.append({
            "code": str(code).zfill(6),
            "ts": ts,
            "price": price,
            "volume": volume,
            "amount": amount,
        })
    return out


def save_1min_kline(db: DatabaseManager, rows: List[dict]) -> int:
    """批量保存 1min K 线，**只 INSERT 新增，已存在跳过**（增量写）。

    Returns:
        新增行数（不含已存在的）。已存在的 ts 完全跳过，不 UPDATE，
        因为 1min K 线历史数据不可变。
    """
    if not rows:
        return 0

    code = rows[0]["code"]

    def _write(session) -> int:
        # 1 次批量查已有 ts（替代 N 次 SELECT）
        existing_ts = {
            ts for (ts,) in session.query(Stock1minKline.ts)
            .filter(Stock1minKline.code == code)
            .all()
        }
        new_rows = [r for r in rows if r["ts"] not in existing_ts]
        if not new_rows:
            return 0
        for r in new_rows:
            session.add(Stock1minKline(data_source="10jqka", **r))
        return len(new_rows)

    try:
        return db._run_write_transaction(f"save_1min_kline[{code}]", _write)
    except Exception as e:
        logger.warning("save_1min_kline %s 失败: %s", code, e)
        return 0


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description="抓取个股 1 分钟分时数据")
    p.add_argument("codes", nargs="*", help="股票代码（不传则全市场）")
    p.add_argument("--max-stocks", type=int, default=None, help="限制股票数")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    db = DatabaseManager()

    if args.codes:
        codes = args.codes
    else:
        states = db.get_sync_states(status="done")
        codes = [s["code"] for s in states]
    if args.max_stocks:
        codes = codes[:args.max_stocks]

    print(f"待抓 {len(codes)} 只", flush=True)
    total, saved, failed, rows_written = len(codes), 0, 0, 0

    import time
    for idx, code in enumerate(codes):
        try:
            data = fetch_1min_kline(code)
            if data:
                fetched = len(data)
                n = save_1min_kline(db, data)
                saved += 1
                rows_written += n
                last_ts = data[-1]["ts"].strftime("%Y-%m-%d %H:%M")
                print(f"[{idx+1}/{total}] {code}: 抓{fetched}/新增{n} (last={last_ts})", flush=True)
            else:
                failed += 1
                print(f"[{idx+1}/{total}] {code}: 无数据", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{idx+1}/{total}] {code}: 失败 - {e}", flush=True)
        if idx < total - 1:
            time.sleep(10.0)

    print(f"完成: total={total} saved={saved} failed={failed} rows={rows_written}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
