# -*- coding: utf-8 -*-
"""抓取同花顺概念板块资金流向数据，存入 stock_concept_fund_flow 表。

数据来源: https://data.10jqka.com.cn/funds/gnzjl/  （按净额降序）
共 385 个概念板块，分 8 页（page 9 起为空），每页 50 条。

用法:
    python -m scripts.scrape_concept_fund_flow            # 抓当前交易日
    python -m scripts.scrape_concept_fund_flow --date 20260613
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

from src.storage import DatabaseManager, StockConceptFundFlow, ConceptDim
from src.utils.trading_date import TradingDate

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.10jqka.com.cn/funds/gnzjl/",
}

BASE_URL = "https://data.10jqka.com.cn/funds/gnzjl/field/je/order/desc/page/{page}/"
MAX_PAGES = 10  # 安全上限，实际第 9 页起为空


def fetch_concept_fund_flow(session: requests.Session | None = None) -> List[dict]:
    """抓取概念板块资金流向的全量数据（按净额降序，约 385 条）。

    Returns:
        list[dict]，每个 dict 含 seq/concept/concept_index/pct_chg/inflow/outflow/
        net_amount/company_count/leader_name/leader_pct/leader_price 字段。
    """
    s = session or requests.Session()
    results: List[dict] = []
    global_seq = 0

    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL.format(page=page)
        try:
            r = s.get(url, headers=HEADERS, timeout=15)
            r.encoding = "gbk"
        except Exception as e:
            logger.warning("抓取第 %d 页失败: %s", page, e)
            break

        rows = _parse_table(r.text)
        if not rows:
            logger.info("第 %d 页无数据，停止翻页", page)
            break

        for concept_code, cells in rows:
            global_seq += 1
            parsed = _parse_row(cells, global_seq, concept_code)
            if parsed:
                results.append(parsed)

        logger.info("第 %d 页抓到 %d 条，累计 %d", page, len(rows), len(results))
        if page < MAX_PAGES:
            time.sleep(5.0)  # 翻页间隔 5 秒，避免触发同花顺限频

    return results


def _parse_table(html: str) -> List[tuple]:
    """从 HTML 中解析资金流向表格的所有数据行。

    Returns:
        list[tuple[str, list[str]]]，每个元素为 (concept_code, cells)。
        cells 为 11 个单元格内容（已去标签）。
    """
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    if not tables:
        return []

    table_html = max(tables, key=lambda t: len(re.findall(r"<tr[^>]*>", t)))
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
    if len(rows) < 3:
        return []

    out: List[tuple] = []
    for row in rows[2:]:  # 跳过 2 行表头
        # 提取概念代码（如 http://q.10jqka.com.cn/gn/detail/code/309062/）
        m = re.search(r"/gn/detail/code/(\d+)/", row)
        concept_code = m.group(1) if m else None
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(cells) < 11:
            continue
        out.append((concept_code, cells))
    return out


def _parse_row(cells: List[str], seq: int, concept_code: Optional[str] = None) -> Optional[dict]:
    """解析一行数据为 dict。

    11 列: [0]序号 [1]行业 [2]行业指数 [3]涨跌幅 [4]流入资金(亿)
          [5]流出资金(亿) [6]净额(亿) [7]公司家数 [8]领涨股
          [9]领涨股涨跌幅 [10]当前价(元)
    """
    concept = cells[1].strip()
    if not concept:
        return None
    return {
        "seq": seq,
        "concept_code": concept_code,
        "concept": concept,
        "concept_index": _float(cells[2]),
        "pct_chg": _pct(cells[3]),
        "inflow": _float(cells[4]),
        "outflow": _float(cells[5]),
        "net_amount": _float(cells[6]),
        "company_count": _int(cells[7]),
        "leader_name": cells[8].strip() or None,
        "leader_pct": _pct(cells[9]),
        "leader_price": _float(cells[10]),
    }


def save_concept_fund_flow(db: DatabaseManager, rows: List[dict], trade_date: date) -> int:
    """批量 upsert 概念资金流向数据，同时维护 concept_dim 维度表。

    - concept_dim: 每行先 upsert (concept_code, concept_name, first_seen_date)
    - stock_concept_fund_flow: 按 (date, concept_code) upsert，不再存 name
    """
    if not rows:
        return 0

    # 过滤掉 concept_code 缺失的行（理论上不会发生）
    valid_rows = [r for r in rows if r.get("concept_code")]
    skipped = len(rows) - len(valid_rows)
    if skipped:
        logger.warning("跳过 %d 行（concept_code 缺失）", skipped)

    def _write(session) -> int:
        # 1) upsert concept_dim
        for row in valid_rows:
            code = row["concept_code"]
            name = row["concept"]
            dim = session.get(ConceptDim, code)
            if dim:
                if dim.concept_name != name:
                    dim.concept_name = name
                    dim.updated_at = datetime.now()
            else:
                session.add(ConceptDim(
                    concept_code=code,
                    concept_name=name,
                    first_seen_date=trade_date,
                ))

        # 2) upsert stock_concept_fund_flow（不存 name）
        for row in valid_rows:
            existing = (
                session.query(StockConceptFundFlow)
                .filter_by(date=trade_date, concept_code=row["concept_code"])
                .first()
            )
            fund_fields = {
                "seq": row["seq"],
                "concept_code": row["concept_code"],
                "concept_index": row["concept_index"],
                "pct_chg": row["pct_chg"],
                "inflow": row["inflow"],
                "outflow": row["outflow"],
                "net_amount": row["net_amount"],
                "company_count": row["company_count"],
                "leader_name": row["leader_name"],
                "leader_pct": row["leader_pct"],
                "leader_price": row["leader_price"],
            }
            if existing:
                for k, v in fund_fields.items():
                    setattr(existing, k, v)
                existing.updated_at = datetime.now()
            else:
                session.add(StockConceptFundFlow(
                    date=trade_date, data_source="10jqka", **fund_fields,
                ))
        return len(valid_rows)

    try:
        return db._run_write_transaction("save_concept_fund_flow", _write)
    except Exception as e:
        logger.warning("save_concept_fund_flow 失败: %s", e)
        return 0


def _float(v: str) -> Optional[float]:
    v = str(v).replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: str) -> Optional[int]:
    v = str(v).replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _pct(v: str) -> Optional[float]:
    v = v.replace("%", "").replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description="抓取同花顺概念板块资金流向")
    p.add_argument("--date", type=str, default=None, help="指定交易日 YYYYMMDD（默认基于 stock_daily 自动判定）")
    p.add_argument("--force", action="store_true", help="强制重抓（默认当天已抓则跳过）")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    db = DatabaseManager()

    if args.date:
        d = date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:8]))
    else:
        session = db.get_session()
        try:
            d = TradingDate.get_target_trade_date(session)
        finally:
            session.close()
    print(f"目标交易日: {d}", flush=True)

    # 当天已抓则跳过（除非 --force）
    if not args.force:
        session = db.get_session()
        try:
            from src.storage import StockConceptFundFlow
            existing = session.query(StockConceptFundFlow).filter_by(date=d).first()
        finally:
            session.close()
        if existing:
            print(f"当日已抓取，跳过 (date={d})。如需重抓请加 --force", flush=True)
            return 0

    data = fetch_concept_fund_flow()
    if not data:
        print("抓取失败：无数据", flush=True)
        return 1
    print(f"抓到 {len(data)} 条概念板块数据", flush=True)

    n = save_concept_fund_flow(db, data, d)
    print(f"已保存 {n} 条到 stock_concept_fund_flow (date={d})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
