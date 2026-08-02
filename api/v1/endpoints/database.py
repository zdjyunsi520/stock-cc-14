# -*- coding: utf-8 -*-
"""
===================================
数据库浏览接口
===================================

职责：
1. 列出本地 SQLite 所有表 + 行数 + 字段定义
2. 分页浏览单张表的数据（只读，无写入）

安全：
- 表名通过白名单校验（必须存在于 sqlite_master）
- 不接受任意 SQL，避免注入
- 单页 size 上限 200
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def _list_table_names(db: DatabaseManager) -> List[str]:
    """从 sqlite_master 拿到所有用户表名（排除 sqlite_*）。"""
    with db.session_scope() as s:
        rows = s.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        ).fetchall()
    return [r[0] for r in rows]


def _get_table_columns(db: DatabaseManager, table: str) -> List[Dict[str, Any]]:
    """PRAGMA table_info 拿字段定义。"""
    with db.session_scope() as s:
        rows = s.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return [
        {
            "name": r[1],
            "type": r[2],
            "not_null": bool(r[3]),
            "default": r[4],
            "primary_key": bool(r[5]),
        }
        for r in rows
    ]


def _count_rows(db: DatabaseManager, table: str) -> int:
    """精确 COUNT，大表会很慢（5297 万行 ≈ 8 分钟）。仅用于小表。"""
    with db.session_scope() as s:
        return int(s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)


def _estimate_rows(db: DatabaseManager, table: str, threshold: int = 100_000) -> tuple[int, bool]:
    """估算行数：小于 threshold 用精确 COUNT，大于则用 MAX(rowid) 近似。

    返回 (count, is_estimate)。WITHOUT ROWID 表回退到精确 COUNT。
    """
    with db.session_scope() as s:
        # 是否 WITHOUT ROWID
        sql_row = s.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": table},
        ).scalar() or ""
        if "WITHOUT ROWID" in sql_row.upper():
            n = int(s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
            return n, False
        # 先快速估算
        est = s.execute(text(f'SELECT MAX(rowid) FROM "{table}"')).scalar()
        if est is None:
            return 0, False
        if est < threshold:
            # 小表精确
            n = int(s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
            return n, False
        return int(est), True


@router.get("/tables", summary="列出所有表")
async def list_tables() -> Dict[str, Any]:
    """返回所有表的名称、行数、字段定义。"""
    db = DatabaseManager()
    names = _list_table_names(db)
    tables: List[Dict[str, Any]] = []
    for name in names:
        try:
            cols = _get_table_columns(db, name)
            n, is_est = _estimate_rows(db, name)
        except Exception as exc:
            logger.warning("table %s inspect failed: %s", name, exc)
            cols, n, is_est = [], 0, False
        tables.append({"name": name, "row_count": n, "row_count_estimated": is_est, "columns": cols})
    tables.sort(key=lambda x: x["row_count"], reverse=True)
    return {"total": len(tables), "tables": tables}


@router.get("/tables/{table_name}/rows", summary="分页浏览表数据")
async def list_rows(
    table_name: str,
    page: int = Query(1, ge=1),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    code: Optional[str] = Query(None, description="股票代码（精确匹配 code 列）"),
    name: Optional[str] = Query(None, description="股票名称（精确匹配 name 列）"),
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD（date 列精确匹配；ts 列按当天范围）"),
    order_by: Optional[str] = Query(None, description="排序字段名"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
) -> Dict[str, Any]:
    """分页浏览单张表的数据。

    只允许 SELECT，不接受任意 SQL。
    精确匹配（=）优先，避免 LIKE '%x%' 全表扫描卡死大表（如 stock_1min_kline 5297 万行）。
    date 参数类型感知：DATE 列直接相等比较，DATETIME 列（如 ts）按当天范围比较。
    """
    db = DatabaseManager()
    names = set(_list_table_names(db))
    if table_name not in names:
        raise HTTPException(status_code=404, detail=f"table {table_name} not found")

    cols = _get_table_columns(db, table_name)
    col_names = [c["name"] for c in cols]
    col_types = {c["name"]: (c["type"] or "").upper() for c in cols}

    # 构造 WHERE（仅精确匹配，按列存在性 + 类型感知）
    params: Dict[str, Any] = {}
    clauses: List[str] = []

    if code and "code" in col_types:
        clauses.append('"code" = :code')
        params["code"] = code
    if name and "name" in col_types:
        clauses.append('"name" = :name')
        params["name"] = name
    if date:
        # 优先 date 列（DATE 类型，SQLite 存为 'YYYY-MM-DD' TEXT）
        if "date" in col_types:
            clauses.append('"date" = :date')
            params["date"] = date
        # 其次 ts 列（DATETIME 类型，存为 'YYYY-MM-DD HH:MM:SS'，按天范围）
        elif "ts" in col_types:
            clauses.append('"ts" >= :ts_start AND "ts" < :ts_end')
            params["ts_start"] = f"{date} 00:00:00"
            params["ts_end"] = f"{date} 23:59:59"

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # 总数：带过滤则精确 COUNT（结果集小）；不带过滤则用估算（大表避免全表扫）
    # 例外：大表（>100万行）带 LIKE 全表扫描，COUNT(*) 极慢（5297万行实测几分钟），
    # 这种情况下跳过 COUNT，改用 LIMIT size+1 探测 has_next，total 返回 None。
    total_estimated = False
    total: Optional[int] = None
    skip_count = False
    if where_sql:
        est_count, _ = _estimate_rows(db, table_name)
        if est_count > 1_000_000:
            # 大表跳过 COUNT
            skip_count = True
            total_estimated = True
        else:
            with db.session_scope() as s:
                total = int(
                    s.execute(text(f'SELECT COUNT(*) FROM "{table_name}" {where_sql}'), params).scalar() or 0
                )
    else:
        total, total_estimated = _estimate_rows(db, table_name)

    # 排序
    order_sql = ""
    if order_by and order_by in col_names:
        order_sql = f' ORDER BY "{order_by}" {order.upper()}'
    else:
        # 默认按第一个字段排序，避免分页乱序
        order_sql = f' ORDER BY "{col_names[0]}" ASC' if col_names else ""

    offset = (page - 1) * size
    # 跳过 COUNT 时多取 1 条用于探测 has_next
    fetch_size = size + 1 if skip_count else size
    sql = f'SELECT * FROM "{table_name}" {where_sql}{order_sql} LIMIT :limit OFFSET :offset'
    params2 = dict(params)
    params2["limit"] = fetch_size
    params2["offset"] = offset

    has_next = False
    with db.session_scope() as s:
        result = s.execute(text(sql), params2)
        rows = [dict(zip(result.keys(), r)) for r in result.fetchall()]
    if skip_count and len(rows) > size:
        has_next = True
        rows = rows[:size]

    # total_pages：跳过 COUNT 时返回 None（前端显示 "?"）
    if total is None:
        total_pages = None
    else:
        total_pages = (total + size - 1) // size if size else 0

    return {
        "table": table_name,
        "columns": col_names,
        "rows": rows,
        "page": page,
        "size": size,
        "total": total,
        "total_estimated": total_estimated,
        "total_pages": total_pages,
        "has_next": has_next,
    }
