# -*- coding: utf-8 -*-
"""
同花顺(THS)数据源 — 概念板块排行 + 成分股。

已验证可用的接口：
- ak.stock_fund_flow_concept()          → 概念资金流向排行（涨跌幅/净额/家数/领涨股）
- ak.stock_board_concept_name_ths()     → 概念板块名称→代码映射
- ak.stock_board_industry_name_ths()    → 行业板块名称→代码映射
- d.10jqka.com.cn/v2/blockrank/{code}/  → 成分股详情（价格/涨跌幅/换手/主力资金/市值）

blockrank 字段映射（已硬编码，勿随意改动）：
  5=代码, 55=名称, 6=昨收, 7=今开, 8=最高, 9=最低, 10=最新价,
  13=成交量, 19=成交额, 199112=涨跌幅, 264648=振幅, 2034120=换手率,
  1968584=市盈率, 3475914=总市值, 3541450=流通市值,
  223=主力流入, 224=主力流出, 225=超大单流入, 226=超大单流出,
  237=大单流入, 238=大单流出, 259=中单流入, 260=中单流出
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .base import BaseFetcher, normalize_stock_code

logger = logging.getLogger(__name__)

# blockrank 字段映射
_BLOCKRANK_FIELDS = {
    "5": "code",
    "55": "name",
    "6": "prev_close",
    "7": "open",
    "8": "high",
    "9": "low",
    "10": "price",
    "13": "volume",
    "19": "amount",
    "199112": "change_pct",
    "264648": "amplitude",
    "2034120": "turnover_rate",
    "1968584": "pe_ratio",
    "3475914": "total_mv",
    "3541450": "circ_mv",
    "223": "main_inflow",
    "224": "main_outflow",
    "225": "super_large_in",
    "226": "super_large_out",
    "237": "large_in",
    "238": "large_out",
    "259": "medium_in",
    "260": "medium_out",
}

_THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://q.10jqka.com.cn/",
}


class ThsFetcher(BaseFetcher):
    """同花顺数据源：概念板块排行 + 成分股。"""

    def __init__(self) -> None:
        self.name = "ThsFetcher"
        self.priority = -1  # 最高优先级，排在所有 fetcher 前面
        self._concept_code_map: Optional[Dict[str, str]] = None
        self._industry_code_map: Optional[Dict[str, str]] = None
        self._code_map_ts: float = 0.0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str):
        import pandas as pd
        return pd.DataFrame()

    def _normalize_data(self, df, stock_code: str):
        import pandas as pd
        return df

    # ------------------------------------------------------------------
    # 概念排行 — 数据来自东财资金流向（data.eastmoney.com，网络可用）
    # ------------------------------------------------------------------
    def get_concept_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        try:
            import akshare as ak

            df = ak.stock_fund_flow_concept()
            if df is None or df.empty:
                return None

            col_pct = "行业-涨跌幅"
            col_name = "行业"
            if col_pct not in df.columns or col_name not in df.columns:
                return None

            df["_pct"] = df[col_pct].astype(float)
            df_sorted = df.sort_values("_pct", ascending=False)

            top = [
                {"name": str(r[col_name]), "change_pct": float(r["_pct"])}
                for _, r in df_sorted.head(n).iterrows()
            ]
            bottom = [
                {"name": str(r[col_name]), "change_pct": float(r["_pct"])}
                for _, r in df_sorted.tail(n).iterrows()
            ]
            logger.info("[ThsFetcher] 概念排行获取成功 top=%s", [t["name"] for t in top[:3]])
            return top, bottom
        except Exception as e:
            logger.warning("[ThsFetcher] 概念排行获取失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 行业排行 — 复用东财资金流向的"行业"维度（概念接口里行业也在）
    # ------------------------------------------------------------------
    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        return None

    # ------------------------------------------------------------------
    # 成分股 — 概念用同花顺详情页，行业用 blockrank 接口
    # ------------------------------------------------------------------
    def get_board_members(
        self,
        board_name: str,
        board_type: str = "concept",
        max_members: int = 100,
    ) -> Optional[List[Dict[str, Any]]]:
        code_map = self._ensure_code_map(board_type)
        board_code = code_map.get(board_name)
        if not board_code:
            logger.debug("[ThsFetcher] 未找到板块代码: %s (type=%s)", board_name, board_type)
            return None

        if board_type == "concept":
            return self._fetch_concept_members(board_name, board_code, max_members)
        return self._fetch_industry_members(board_name, board_code, max_members)

    # ------------------------------------------------------------------
    # 概念成分股 — 从详情页提取 88xxxx 指数代码 → blockrank 获取完整列表
    # ------------------------------------------------------------------
    def _fetch_concept_members(
        self, board_name: str, board_code: str, max_members: int
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            index_code = self._resolve_concept_index_code(board_code)
            if index_code:
                items = self._fetch_blockrank(index_code, max_members)
                if items:
                    return self._build_member_list(items, board_name, "concept", "ths_blockrank")

            # fallback: 详情页首屏 10 条
            return self._fetch_concept_members_fallback(board_name, board_code, max_members)
        except Exception as e:
            logger.warning("[ThsFetcher] 概念成分股获取失败 board=%s: %s", board_name, e)
            return None

    def _resolve_concept_index_code(self, board_code: str) -> Optional[str]:
        """从概念详情页提取 88xxxx 板块指数代码。"""
        url = f"http://q.10jqka.com.cn/gn/detail/code/{board_code}/"
        try:
            resp = requests.get(url, headers=_THS_HEADERS, timeout=15)
            resp.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            hq = soup.find("div", class_="board-hq")
            if hq:
                span = hq.find("h3").find("span") if hq.find("h3") else None
                if span:
                    code = span.text.strip()
                    if code.startswith("88"):
                        logger.info("[ThsFetcher] 板块指数代码: %s -> %s", board_code, code)
                        return code
        except Exception as e:
            logger.debug("[ThsFetcher] 提取板块指数代码失败: %s", e)
        return None

    def _build_member_list(
        self, items: List[Dict], board_name: str, board_type: str, source: str
    ) -> List[Dict[str, Any]]:
        result = []
        for item in items:
            stock_code = normalize_stock_code(str(item.get("code") or ""))
            if not stock_code:
                continue
            change_pct = _safe_float(item.get("change_pct"))
            main_in = _safe_float(item.get("main_inflow"))
            main_out = _safe_float(item.get("main_outflow"))
            result.append({
                "code": stock_code,
                "name": str(item.get("name") or ""),
                "board": board_name,
                "board_type": board_type,
                "source": source,
                "change_pct": change_pct,
                "price": _safe_float(item.get("price")),
                "volume": _safe_float(item.get("volume")),
                "amount": _safe_float(item.get("amount")),
                "turnover_rate": _safe_float(item.get("turnover_rate")),
                "amplitude": _safe_float(item.get("amplitude")),
                "circ_mv": _safe_float(item.get("circ_mv")),
                "total_mv": _safe_float(item.get("total_mv")),
                "pe_ratio": _safe_float(item.get("pe_ratio")),
                "main_net_inflow": (main_in - main_out) if main_in is not None and main_out is not None else None,
                "main_inflow": main_in,
                "main_outflow": main_out,
            })
        logger.info("[ThsFetcher] 成分股获取成功 board=%s count=%d", board_name, len(result))
        return result

    def _fetch_concept_members_fallback(
        self, board_name: str, board_code: str, max_members: int
    ) -> Optional[List[Dict[str, Any]]]:
        """详情页首屏（最多 10 条，按涨跌幅降序）。"""
        url = f"http://q.10jqka.com.cn/gn/detail/code/{board_code}/"
        try:
            resp = requests.get(url, headers=_THS_HEADERS, timeout=15)
            resp.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            table = soup.find("table", class_="m-pager-table")
            if not table:
                return None
            result = []
            for row in table.find("tbody").find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                stock_code = normalize_stock_code(cells[1].text.strip())
                if not stock_code:
                    continue
                if len(result) >= max_members:
                    break
                result.append({
                    "code": stock_code,
                    "name": cells[2].text.strip(),
                    "board": board_name,
                    "board_type": "concept",
                    "source": "ths_concept_detail",
                    "change_pct": _safe_float(cells[4].text.strip()),
                    "price": _safe_float(cells[3].text.strip()),
                })
            if not result:
                return None
            logger.info("[ThsFetcher] 概念成分股(fallback) board=%s count=%d", board_name, len(result))
            return result
        except Exception as e:
            logger.warning("[ThsFetcher] 概念成分股fallback失败 board=%s: %s", board_name, e)
            return None

    # ------------------------------------------------------------------
    # 行业成分股 — blockrank 接口（88xxxx 行业代码直接可用）
    # ------------------------------------------------------------------
    def _fetch_industry_members(
        self, board_name: str, board_code: str, max_members: int
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            items = self._fetch_blockrank(board_code, max_members)
            if not items:
                return None
            return self._build_member_list(items, board_name, "industry", "ths_blockrank")
        except Exception as e:
            logger.warning("[ThsFetcher] 行业成分股获取失败 board=%s: %s", board_name, e)
            return None

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _ensure_code_map(self, board_type: str) -> Dict[str, str]:
        ttl = 3600
        if self._concept_code_map and (time.time() - self._code_map_ts < ttl):
            return self._concept_code_map if board_type != "industry" else (self._industry_code_map or {})

        try:
            import akshare as ak

            self._concept_code_map = {}
            concept_df = ak.stock_board_concept_name_ths()
            if concept_df is not None and not concept_df.empty:
                for _, row in concept_df.iterrows():
                    name = str(row.get("name") or "").strip()
                    code = str(row.get("code") or "").strip()
                    if name and code:
                        self._concept_code_map[name] = code

            self._industry_code_map = {}
            ind_df = ak.stock_board_industry_name_ths()
            if ind_df is not None and not ind_df.empty:
                for _, row in ind_df.iterrows():
                    name = str(row.get("name") or "").strip()
                    code = str(row.get("code") or "").strip()
                    if name and code:
                        self._industry_code_map[name] = code

            self._code_map_ts = time.time()
            logger.info(
                "[ThsFetcher] 板块代码映射已加载: concept=%d, industry=%d",
                len(self._concept_code_map),
                len(self._industry_code_map),
            )
        except Exception as e:
            logger.warning("[ThsFetcher] 板块代码映射加载失败: %s", e)

        return self._concept_code_map if board_type != "industry" else (self._industry_code_map or {})

    @staticmethod
    def _fetch_blockrank(board_code: str, max_members: int) -> List[Dict[str, Any]]:
        url = f"https://d.10jqka.com.cn/v2/blockrank/{board_code}/199112/d{max_members}.js"
        resp = requests.get(url, headers=_THS_HEADERS, timeout=15)
        resp.raise_for_status()

        match = re.search(r"\((.*)\)", resp.text, re.DOTALL)
        if not match:
            return []

        data = json.loads(match.group(1))
        raw_items = data.get("items", [])
        if not raw_items:
            return []

        result = []
        for raw in raw_items:
            item = {}
            for field_id, field_name in _BLOCKRANK_FIELDS.items():
                val = raw.get(field_id)
                if val is not None:
                    item[field_name] = val
            if item.get("code"):
                result.append(item)
        return result


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        return v if v == v else None  # NaN check
    except (TypeError, ValueError):
        return None
