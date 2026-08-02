# -*- coding: utf-8 -*-
"""股票过滤公共模块 — 项目级统一的板块/名称黑名单。

黑名单规则：
- 板块黑名单：300/301 创业板、688 科创板、4*/8* 北交所
- 名称黑名单：ST/*ST/退市

所有 src/services/* 与 main.py 中的过滤逻辑应统一调用本模块，
避免规则漂移（如 daily_data_sync_service 历史上只过滤 688+ST，漏掉 300/301/4/8）。

设计原则：
- 空名称一律不排除（避免误杀未查到名称的股票）
- 不导出"业务级"过滤（如选股策略的成交额/价格阈值）—— 只做硬黑名单
- 纯函数、无副作用、无 IO，可被任何层调用
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

# 板块黑名单前缀：300/301 创业板、688 科创板、4/8 北交所
BOARD_BLACKLIST_PREFIXES: Tuple[str, ...] = ("300", "301", "688", "4", "8")


def is_excluded_board(code: str) -> bool:
    """是否属于板块黑名单（创业板/科创板/北交所）。

    Args:
        code: 6 位股票代码（未补零会按字面匹配，调用方应先 zfill(6)）

    Returns:
        True 表示该 code 属于板块黑名单
    """
    if not code:
        return False
    return code.startswith(BOARD_BLACKLIST_PREFIXES)


def is_excluded_by_name(name: str) -> bool:
    """是否属于名称黑名单（ST/*ST/退市）。

    空名称返回 False（避免误杀未查到名称的股票）。

    Args:
        name: 股票名称（如 "*ST罗顿"、"XX退"）

    Returns:
        True 表示该名称属于黑名单
    """
    if not name:
        return False
    if "ST" in name.upper():
        return True
    if "退" in name:
        return True
    return False


def get_exclude_reason(code: str, name: str = "") -> str:
    """返回排除原因；空串表示不排除。

    Args:
        code: 6 位股票代码
        name: 股票名称（可选）

    Returns:
        "板块黑名单" / "ST/退市({name})" / ""
    """
    if is_excluded_board(code):
        return "板块黑名单"
    if is_excluded_by_name(name):
        return f"ST/退市({name})"
    return ""


def should_exclude(code: str, name: str = "") -> bool:
    """是否应排除（板块黑名单 OR 名称黑名单）。最常用的快捷接口。"""
    return is_excluded_board(code) or is_excluded_by_name(name)


def filter_codes(
    codes: Iterable[str],
    name_map: Optional[dict] = None,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """批量过滤股票池。

    Args:
        codes: 待过滤的 code 列表
        name_map: {code: name} 映射；为 None 时只做板块过滤

    Returns:
        (kept_codes, excluded_detail)
        - kept_codes: 通过过滤的 code 列表（保持原顺序）
        - excluded_detail: [(code, reason), ...] 被剔除的明细
    """
    kept: List[str] = []
    excluded: List[Tuple[str, str]] = []
    name_map = name_map or {}
    for code in codes:
        name = name_map.get(code, "")
        reason = get_exclude_reason(code, name)
        if reason:
            excluded.append((code, reason))
        else:
            kept.append(code)
    return kept, excluded


def filter_codes_to_set(
    codes: Iterable[str],
    name_map: Optional[dict] = None,
) -> Set[str]:
    """批量过滤，返回保留 code 的集合（用于快速 membership 测试）。"""
    kept, _ = filter_codes(codes, name_map)
    return set(kept)
