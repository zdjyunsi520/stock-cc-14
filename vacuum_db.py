#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vacuum_db.py — 回收 SQLite 数据库的空闲空间

删除大量数据后，数据库文件不会自动缩小，需要 VACUUM 才能释放磁盘空间。

执行前必须停掉所有占用数据库的进程（main.py / bot / webui / API server）。
预计耗时 30-60 分钟（3400 万行级别）。

用法:
    python vacuum_db.py                          # 默认 data/stock_analysis.db
    python vacuum_db.py D:\\path\\to\\other.db   # 指定其他数据库
"""

import os
import sys
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "stock_analysis.db"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB

    if not db_path.exists():
        print(f"[错误] 数据库文件不存在: {db_path}")
        return 1

    size_before = db_path.stat().st_size
    print("=" * 60)
    print(f"数据库: {db_path}")
    print(f"VACUUM 前大小: {size_before / 1024**3:.2f} GB")
    print("=" * 60)
    print("开始 VACUUM（可能耗时 30-60 分钟，期间无进度输出，请耐心等待）")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.stdout.flush()

    t0 = time.time()
    try:
        # isolation_level=None 走 autocommit，VACUUM 不能在事务里跑
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        print(f"\n[错误] {e}")
        if "locked" in msg:
            print("数据库被锁定 —— 请先关闭所有占用进程：")
            print("  python main.py / bot / webui / API server / 其他 python -c 进程")
        elif "disk" in msg or "space" in msg:
            print("磁盘空间不足 —— VACUUM 需要约 1 倍当前数据库大小的临时空间")
        return 1

    elapsed = time.time() - t0
    size_after = db_path.stat().st_size
    saved = size_before - size_after

    print(f"\n完成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"用时: {elapsed / 60:.1f} 分钟")
    print(f"VACUUM 后大小: {size_after / 1024**3:.2f} GB")
    print(f"节省空间: {saved / 1024**3:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
