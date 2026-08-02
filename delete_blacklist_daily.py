#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delete_blacklist_daily.py — 清理 stock_daily 表中黑名单股票的历史数据

黑名单规则（与 --sync-incremental 过滤逻辑一致）：
- 板块黑名单：300/301 创业板、688 科创板、4*/8* 北交所
- 名称黑名单：ST/*ST/退市

默认 dry-run（仅显示将删除多少），加 --force 才真删。
删除完成后，建议执行 vacuum_db.py 回收磁盘空间。

用法:
    python delete_blacklist_daily.py             # dry-run，仅显示数量
    python delete_blacklist_daily.py --force     # 真删
    python delete_blacklist_daily.py --db D:\\path\\to\\other.db --force
"""

import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "stock_analysis.db"

# 板块黑名单前缀：300/301 创业板、688 科创板、4/8 北交所
BOARD_PREFIXES = ("300", "301", "688", "4", "8")


def is_excluded_board(code: str) -> bool:
    return code.startswith(BOARD_PREFIXES)


def is_excluded_by_name(name: str) -> bool:
    if not name:
        return False
    return "ST" in name.upper() or "退" in name


def main() -> int:
    args = sys.argv[1:]
    force = "--force" in args
    db_path = DEFAULT_DB

    # 解析 --db 参数
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            db_path = Path(args[i + 1])
        elif a.startswith("--db="):
            db_path = Path(a[len("--db="):])

    if not db_path.exists():
        print(f"[错误] 数据库文件不存在: {db_path}")
        return 1

    mode_label = "真删" if force else "DRY-RUN（仅显示数量，加 --force 才真删）"
    print("=" * 60)
    print(f"数据库: {db_path}")
    print(f"模式: {mode_label}")
    print("=" * 60)
    sys.stdout.flush()

    t0 = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        # 1. 统计板块黑名单
        board_count = conn.execute(
            "SELECT COUNT(*) FROM stock_daily "
            "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' "
            "OR code LIKE '4%' OR code LIKE '8%'"
        ).fetchone()[0]
        board_codes = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM stock_daily "
            "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' "
            "OR code LIKE '4%' OR code LIKE '8%'"
        ).fetchone()[0]

        # 2. 统计 ST/退市（通过 sync_state 关联找名称）
        st_rows = conn.execute(
            "SELECT DISTINCT k.code FROM stock_daily k "
            "JOIN stock_daily_sync_state s ON k.code = s.code "
            "WHERE UPPER(s.code_name) LIKE '%ST%' OR s.code_name LIKE '%退%'"
        ).fetchall()
        st_codes = [r[0] for r in st_rows]
        st_count = 0
        if st_codes:
            placeholders = ",".join("?" for _ in st_codes)
            st_count = conn.execute(
                f"SELECT COUNT(*) FROM stock_daily WHERE code IN ({placeholders})",
                st_codes,
            ).fetchone()[0]

        # 3. 全表对照
        total = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]

        print(f"全表 stock_daily: {total:,} 行")
        print(f"板块黑名单: {board_codes} 只股票, {board_count:,} 行")
        print(f"ST/退市:    {len(st_codes)} 只股票, {st_count:,} 行")
        print(f"预计删除:   {board_count + st_count:,} 行 ({(board_count+st_count)/total*100:.1f}%)")
        print(f"剩余:       {total - board_count - st_count:,} 行")
        sys.stdout.flush()

        if not force:
            print("\n[DRY-RUN] 未执行删除。确认无误后加 --force 真删。")
            return 0

        if board_count + st_count == 0:
            print("\n无数据可删。")
            return 0

        # === 真删 ===
        print(f"\n[FORCE] 开始删除（autocommit，{time.strftime('%Y-%m-%d %H:%M:%S')}）...")
        sys.stdout.flush()
        conn.isolation_level = None  # autocommit

        del_board = conn.execute(
            "DELETE FROM stock_daily "
            "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' "
            "OR code LIKE '4%' OR code LIKE '8%'"
        ).rowcount

        del_st = 0
        if st_codes:
            for i in range(0, len(st_codes), 500):
                batch = st_codes[i: i + 500]
                placeholders = ",".join("?" for _ in batch)
                del_st += conn.execute(
                    f"DELETE FROM stock_daily WHERE code IN ({placeholders})",
                    batch,
                ).rowcount

        elapsed = time.time() - t0
        print(f"\n[完成] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"用时: {elapsed:.1f}s")
        print(f"删除板块黑名单: {del_board:,} 行")
        print(f"删除 ST/退市:    {del_st:,} 行")
        print(f"合计删除:        {del_board + del_st:,} 行")

        # 验证
        leftover_board = conn.execute(
            "SELECT COUNT(*) FROM stock_daily "
            "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' "
            "OR code LIKE '4%' OR code LIKE '8%'"
        ).fetchone()[0]
        print(f"验证: 板块黑名单剩余 {leftover_board} 行")
        print("\n建议下一步: python vacuum_db.py  (回收磁盘空间)")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
