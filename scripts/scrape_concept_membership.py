# -*- coding: utf-8 -*-
"""抓取同花顺概念板块成分股，存入 stock_concept_membership 表。

数据来源: 同花顺 q.10jqka.com.cn/gn/detail/code/{concept_code}/
通过 data_provider.ths_fetcher.ThsFetcher 拉取，每个题材返回最多 200 只成分股。

用法:
    python -m scripts.scrape_concept_membership              # 全量（377个题材，约40分钟）
    python -m scripts.scrape_concept_membership --limit 5    # 仅前5个题材（测试用）
    python -m scripts.scrape_concept_membership --codes 309062,301715  # 指定题材code
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from src.storage import DatabaseManager, StockConceptMembership

logger = logging.getLogger(__name__)


def fetch_concept_members(fetcher, concept_code: str, concept_name: str, max_members: int = 200):
    """拉单个题材的成分股列表。

    直接调 ThsFetcher 私有方法，绕过 code_map（concept_dim 已提供 code）。
    Returns: list[dict]，字段 code/name。
    """
    return fetcher._fetch_concept_members(concept_name, concept_code, max_members)


def save_membership(db: DatabaseManager, concept_code: str, concept_name: str, members: List[dict]) -> int:
    """入库：先删该题材旧记录，再批量插。返回新增行数。"""
    if not members:
        return 0

    now = datetime.now()

    def _write(session) -> int:
        # 删旧（同一题材成分股会变，全量替换）
        deleted = session.query(StockConceptMembership).filter(
            StockConceptMembership.concept_code == concept_code
        ).delete()
        # 批量插
        for m in members:
            code = (m.get("code") or "").zfill(6)
            if not code or len(code) != 6:
                continue
            session.add(StockConceptMembership(
                code=code,
                concept_code=concept_code,
                concept_name=concept_name,
                fetched_at=now,
            ))
        return len(members)

    try:
        return db._run_write_transaction(f"save_membership[{concept_code}]", _write)
    except Exception as e:
        logger.warning("save_membership %s 失败: %s", concept_code, e)
        return 0


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="抓取概念板块成分股")
    p.add_argument("--limit", type=int, default=None, help="限制题材数（测试用）")
    p.add_argument("--codes", type=str, default=None, help="指定题材code，逗号分隔（如 309062,301715）")
    p.add_argument("--max-members", type=int, default=200, help="每题材最多拉多少只（默认200）")
    args = p.parse_args()

    from data_provider.ths_fetcher import ThsFetcher

    db = DatabaseManager()
    fetcher = ThsFetcher()

    # 取目标题材列表
    session = db.get_session()
    try:
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
            rows = session.execute(text(
                "SELECT concept_code, concept_name FROM concept_dim WHERE concept_code IN :codes"
            ).bindparams(__import__("sqlalchemy").bindparam("codes", expanding=True)), {"codes": codes}).fetchall()
        else:
            sql = "SELECT concept_code, concept_name FROM concept_dim ORDER BY concept_code"
            if args.limit:
                sql += f" LIMIT {int(args.limit)}"
            rows = session.execute(text(sql)).fetchall()
    finally:
        session.close()

    total = len(rows)
    print(f"待拉题材: {total} 个 (每题材最多 {args.max_members} 只)", flush=True)

    succeeded, failed, total_members = 0, 0, 0
    for idx, (concept_code, concept_name) in enumerate(rows):
        try:
            members = fetch_concept_members(fetcher, concept_code, concept_name, args.max_members)
            if members:
                n = save_membership(db, concept_code, concept_name, members)
                succeeded += 1
                total_members += n
                print(f"[{idx+1}/{total}] {concept_code} {concept_name}: {n}只", flush=True)
            else:
                failed += 1
                print(f"[{idx+1}/{total}] {concept_code} {concept_name}: 无数据", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{idx+1}/{total}] {concept_code} {concept_name}: 失败 - {e}", flush=True)
        # 同花顺限流：每题材间隔 1.5s（ThsFetcher 内部已分页，这里只控题材间）
        if idx < total - 1:
            time.sleep(1.5)

    print(f"\n完成: 题材 {total} 成功 {succeeded} 失败 {failed} → 入库 {total_members} 行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
