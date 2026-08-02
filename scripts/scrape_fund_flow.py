# -*- coding: utf-8 -*-
"""抓取同花顺个股资金流向历史数据，存入 stock_fund_flow 表。

用法:
    python -m scripts.scrape_fund_flow 603477 600519 000001   # 指定股票
    python -m scripts.scrape_fund_flow --all                   # 全市场(从 sync_state 取已同步股票)
    python -m scripts.scrape_fund_flow --all --max-stocks 100  # 限制数量
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage import DatabaseManager, StockFundFlow

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://stockpage.10jqka.com.cn/",
}

FUNDS_URL = "https://stockpage.10jqka.com.cn/{code}/funds/"
REALTIME_URL = "https://stockpage.10jqka.com.cn/spService/{code}/Funds/realFunds/free/1/"


def fetch_fund_flow(code: str, session: requests.Session | None = None) -> List[dict]:
    """从同花顺抓取单只股票的资金流向历史数据 + 当日流入流出明细。"""
    s = session or requests.Session()
    url = FUNDS_URL.format(code=code)
    r = s.get(url, headers=HEADERS, timeout=15)
    r.encoding = "utf-8"

    tables = re.findall(r"<table[^>]*>(.*?)</table>", r.text, re.DOTALL)
    if not tables:
        return []

    table_html = max(tables, key=lambda t: len(re.findall(r"<tr[^>]*>", t)))
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
    if len(rows) < 3:
        return []

    results = []
    for row in rows[2:]:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(cells) < 14:
            continue
        try:
            results.append(_parse_row(code, cells))
        except (ValueError, IndexError):
            continue

    if not results:
        return results

    # 当日数据补充流入流出明细（realFunds API）
    realtime = _fetch_realtime(code, s)
    if realtime:
        today_row = results[0]  # 第一行是最新日期
        today_row.update(realtime)

    return results


def _fetch_realtime(code: str, s: requests.Session) -> dict | None:
    """调 realFunds API 获取当日大/中/小单流入流出明细。"""
    try:
        url = REALTIME_URL.format(code=code)
        r = s.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200 or not r.text:
            return None
        import json
        d = json.loads(r.text)
        flash = d.get("flash", [])
        title = d.get("title", {})
        if len(flash) < 6:
            return None

        # flash 顺序: [大单流出, 中单流出, 小单流出, 小单流入, 中单流入, 大单流入]
        return {
            "big_outflow": _float(flash[0]["sr"]),
            "mid_outflow": _float(flash[1]["sr"]),
            "small_outflow": _float(flash[2]["sr"]),
            "small_inflow": _float(flash[3]["sr"]),
            "mid_inflow": _float(flash[4]["sr"]),
            "big_inflow": _float(flash[5]["sr"]),
            "total_outflow": _float(str(title.get("zlc", ""))),
            "total_inflow": _float(str(title.get("zlr", ""))),
        }
    except Exception as e:
        logger.debug("realFunds %s 失败: %s", code, e)
        return None


def _parse_row(code: str, cells: list[str]) -> dict:
    """解析一行资金流向数据。

    14列: [0]日期 [1]收盘 [2]涨跌幅 [3]净流入 [4]5日主力
          [5]- [6]% [7]-  (大单主力的两级表头占位)
          [8]大单净额 [9]大单占比 [10]中单净额 [11]中单占比
          [12]小单净额 [13]小单占比
    """
    date_str = cells[0]
    d = date(
        int(date_str[:4]),
        int(date_str[4:6]),
        int(date_str[6:8]),
    )
    return {
        "code": code,
        "date": d,
        "close": _float(cells[1]),
        "pct_chg": _pct(cells[2]),
        "net_flow": _float(cells[3]),
        "main_net_5d": _float(cells[4]),
        "big_net": _float(cells[8]),
        "big_pct": _pct(cells[9]),
        "mid_net": _float(cells[10]),
        "mid_pct": _pct(cells[11]),
        "small_net": _float(cells[12]),
        "small_pct": _pct(cells[13]),
    }


def _float(v: str) -> float | None:
    v = str(v).replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _pct(v: str) -> float | None:
    v = v.replace("%", "").replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def save_fund_flow(db: DatabaseManager, rows: List[dict]) -> int:
    """批量保存资金流向数据，计算派生指标，已存在则更新。"""
    if not rows:
        return 0

    # 计算派生指标: strength, big_ratio, big_consecutive
    _compute_derived(rows)

    def _write(session) -> int:
        for row in rows:
            existing = (
                session.query(StockFundFlow)
                .filter_by(code=row["code"], date=row["date"])
                .first()
            )
            if existing:
                for k, v in row.items():
                    if k not in ("code", "date"):
                        setattr(existing, k, v)
                existing.updated_at = datetime.now()
            else:
                session.add(StockFundFlow(**row, data_source="10jqka"))
        return len(rows)

    try:
        return db._run_write_transaction(f"save_fund_flow[{rows[0]['code']}]", _write)
    except Exception as e:
        logger.warning("save_fund_flow %s 失败: %s", rows[0]["code"], e)
        return 0


def _compute_derived(rows: List[dict]) -> None:
    """计算派生指标: strength, big_ratio, big_consecutive。"""
    # rows 按日期降序（最新在前），反转后按时间正序计算连续天数
    sorted_rows = sorted(rows, key=lambda r: r["date"])

    for row in sorted_rows:
        total_turnover = (row.get("total_inflow") or 0) + (row.get("total_outflow") or 0)
        big_turnover = (row.get("big_inflow") or 0) + (row.get("big_outflow") or 0)

        # strength = big_net / (总流入 + 总流出)
        if total_turnover > 0:
            row["strength"] = round((row.get("big_net") or 0) / total_turnover, 4)
        else:
            row["strength"] = None

        # big_ratio = (大单流入 + 大单流出) / (总流入 + 总流出)
        if total_turnover > 0:
            row["big_ratio"] = round(big_turnover / total_turnover, 4)
        else:
            row["big_ratio"] = None

    # big_consecutive: 从旧到新，big_net > 0 则累加，否则归零
    consec = 0
    for i, row in enumerate(sorted_rows):
        big_net = row.get("big_net")
        if big_net is not None and big_net > 0:
            consec += 1
        else:
            consec = 0
        row["big_consecutive"] = consec


def scrape_codes(
    codes: List[str],
    *,
    interval: float = 3.0,
    max_stocks: int | None = None,
) -> dict:
    """批量抓取多只股票的资金流向。"""
    db = DatabaseManager()
    session = requests.Session()
    session.headers.update(HEADERS)

    if max_stocks:
        codes = codes[:max_stocks]

    total = len(codes)
    saved = 0
    failed = 0
    total_rows = 0
    print(f"开始抓取 {total} 只股票资金流向数据，间隔 {interval}s ...")

    for idx, code in enumerate(codes):
        try:
            rows = fetch_fund_flow(code, session)
            if rows:
                n = save_fund_flow(db, rows)
                saved += 1
                total_rows += n
                msg = f"[{idx + 1}/{total}] {code}: {n} 条已保存"
                logger.info(msg)
                print(msg, flush=True)
            else:
                failed += 1
                msg = f"[{idx + 1}/{total}] {code}: 无数据"
                logger.warning(msg)
                print(msg, flush=True)
        except Exception as e:
            failed += 1
            msg = f"[{idx + 1}/{total}] {code}: 失败 - {e}"
            logger.warning(msg)
            print(msg, flush=True)

        if idx < total - 1:
            time.sleep(interval)

    result = {"total": total, "saved": saved, "failed": failed, "rows": total_rows}
    summary = f"资金流向抓取完成: total={total} saved={saved} failed={failed} rows={total_rows}"
    logger.info(summary)
    print(summary, flush=True)
    return result


def get_all_codes(db: DatabaseManager) -> List[str]:
    """从 sync_state 获取所有已完成同步的股票代码。"""
    states = db.get_sync_states(status="done")
    return [s["code"] for s in states]


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # 强制刷新输出
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    codes: List[str] = []
    max_stocks = None

    if "--all" in args:
        db = DatabaseManager()
        codes = get_all_codes(db)
        logger.info("从数据库获取 %d 只股票", len(codes))
        args = [a for a in args if a != "--all"]

    for a in args:
        if a.startswith("--max-stocks="):
            max_stocks = int(a.split("=")[1])
        elif a.startswith("--max-stocks"):
            pass
        elif not a.startswith("-"):
            codes.append(a)

    if not codes:
        print("错误: 未指定股票代码，使用 --all 或指定代码")
        return

    result = scrape_codes(codes, interval=3.0, max_stocks=max_stocks)
    print(f"完成: total={result['total']} saved={result['saved']} "
          f"failed={result['failed']} rows={result['rows']}")


if __name__ == "__main__":
    main()
