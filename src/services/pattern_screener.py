# -*- coding: utf-8 -*-
"""规律选股器 — 基于涨跌基因分析发现的规律筛选股票，支持按交易日缓存。"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                         "data", "pattern_cache")

CONCEPT_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                  "data", "concept_cache")


@dataclass
class PatternScreenerConfig:
    min_price: float = 0.0            # 最低价格
    max_price: float = 99999.0        # 最高价格
    min_vol_ratio: float = 0.8        # 最低量比
    max_ret5: float = 20.0            # 5日涨幅上限（排除追高）
    concept_top_n: int = 20           # 取前N个热点概念
    concept_min_days: int = 3         # 概念至少连续出现几天
    max_candidates: int = 25          # 最多返回候选数
    lookback_days: int = 15           # 加载天数


@dataclass
class PatternCandidate:
    code: str
    name: str = ""
    close: float = 0.0
    vol_ratio: float = 0.0
    ret3: float = 0.0
    ret5: float = 0.0
    up_days_5: int = 0
    dist_ma10: float = 0.0
    consecutive_up: int = 0
    themes: List[str] = field(default_factory=list)
    score: float = 0.0
    wash_score: int = 0          # 洗盘评分（0-20）
    wash_detail: str = ""        # 洗盘特征摘要

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PatternCandidate":
        return cls(**d)


@dataclass
class PatternCacheEntry:
    """单日规律缓存。"""
    date_key: str                              # 如 "20260605"
    hot_themes: List[List[Any]]                # [[概念名, 天数], ...]
    candidates: List[Dict[str, Any]]           # [PatternCandidate.to_dict(), ...]
    analysis_time: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "PatternCacheEntry":
        d = json.loads(raw)
        return cls(**d)


class PatternScreener:
    """基于涨跌规律的选股器，支持按交易日缓存。"""

    def __init__(
        self,
        *,
        db: Optional[DatabaseManager] = None,
        theme_universe_provider: Optional[Callable[[], Dict[str, Sequence[str]]]] = None,
        stock_name_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._theme_universe_provider = theme_universe_provider
        self._stock_name_provider = stock_name_provider

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def screen(
        self,
        config: Optional[PatternScreenerConfig] = None,
        date_key: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Tuple[List[PatternCandidate], List[Tuple[str, int]]]:
        """
        选股入口。
        date_key: 指定日期如 '20260605'，None 则取数据库最新交易日。
        force_refresh: 强制重新分析（忽略缓存）。
        返回: (候选列表, 热点概念列表)
        """
        config = config or PatternScreenerConfig()

        # 确定日期 key
        if date_key is None:
            date_key = self._get_latest_trading_date()
        if not date_key:
            logger.warning("[PatternScreen] 数据库中无交易数据")
            return [], []

        # 标准化为 YYYYMMDD
        date_key = date_key.replace("-", "")
        display_date = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"

        # 检查缓存
        if not force_refresh:
            cached = self._load_cache(date_key)
            if cached:
                logger.info("[PatternScreen] 命中缓存: %s (%s)", date_key, display_date)
                candidates = [PatternCandidate.from_dict(c) for c in cached.candidates]
                hot_themes = [(item[0], item[1]) for item in cached.hot_themes]
                return candidates, hot_themes

        logger.info("[PatternScreen] 未命中缓存，开始实时分析: %s (%s)", date_key, display_date)

        # Step 1: 找出持续热点概念
        hot_themes = self._find_persistent_themes(config, date_key=date_key)
        if not hot_themes:
            logger.warning("[PatternScreen] 无持续热点概念")
            return [], []

        logger.info("[PatternScreen] 持续热点概念: %s", [t for t, _ in hot_themes])

        # Step 2: 获取热点概念成分股
        theme_stocks, stock_themes, stock_names = self._load_theme_members(hot_themes)

        # Step 3: 加载日线 + 指标计算 + 筛选评分
        df = self.db.get_bulk_daily_data(days=config.lookback_days)
        if df.empty:
            return [], hot_themes

        candidates = self._score_and_filter(df, stock_themes, stock_names, config)

        logger.info("[PatternScreen] 筛选结果: %d 只", len(candidates))

        # Step 4: 写入缓存
        from datetime import datetime
        cache_entry = PatternCacheEntry(
            date_key=date_key,
            hot_themes=[[name, days] for name, days in hot_themes],
            candidates=[c.to_dict() for c in candidates],
            analysis_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._save_cache(date_key, cache_entry)

        return candidates, hot_themes

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_path(date_key: str) -> str:
        return os.path.join(CACHE_DIR, f"pattern_{date_key}.json")

    def _load_cache(self, date_key: str) -> Optional[PatternCacheEntry]:
        path = self._cache_path(date_key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return PatternCacheEntry.from_json(f.read())
        except Exception as exc:
            logger.warning("[PatternScreen] 缓存读取失败: %s", exc)
            return None

    def _save_cache(self, date_key: str, entry: PatternCacheEntry) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = self._cache_path(date_key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(entry.to_json())
            logger.info("[PatternScreen] 缓存已保存: %s", path)
        except Exception as exc:
            logger.warning("[PatternScreen] 缓存写入失败: %s", exc)

    def list_cached_dates(self) -> List[str]:
        """列出所有已缓存的日期 key。"""
        if not os.path.isdir(CACHE_DIR):
            return []
        files = [f for f in os.listdir(CACHE_DIR) if f.startswith("pattern_") and f.endswith(".json")]
        dates = [f.replace("pattern_", "").replace(".json", "") for f in files]
        return sorted(dates, reverse=True)

    # ------------------------------------------------------------------
    # 获取数据库最新交易日
    # ------------------------------------------------------------------

    def _get_latest_trading_date(self) -> Optional[str]:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                               "data", "stock_analysis.db")
        if not os.path.exists(db_path):
            return None
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily"
            ).fetchone()
            conn.close()
            if row and row[0]:
                # 转为 YYYYMMDD
                return str(row[0]).replace("-", "")[:8]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Step 1: 找出持续热点概念
    # ------------------------------------------------------------------

    def _find_persistent_themes(self, config: PatternScreenerConfig,
                                  date_key: str = "") -> List[Tuple[str, int]]:
        """从涨跌基因分析中提取连续多天出现的概念。"""
        df = self.db.get_bulk_daily_data(days=config.lookback_days)
        if df.empty:
            return []

        metrics_df = self._compute_basic_metrics(df)
        if metrics_df.empty:
            return []

        trading_dates = sorted(metrics_df["date"].unique(), reverse=True)[:5]

        theme_universe = self._load_theme_universe(config.concept_top_n, date_key=date_key)
        if not theme_universe:
            return []

        concept_day_count: Counter = Counter()
        for d in trading_dates:
            day_df = metrics_df[(metrics_df["date"] == d) & (metrics_df["pct_chg"] >= 3.0)]
            day_concepts: Counter = Counter()
            for _, row in day_df.iterrows():
                for t in theme_universe.get(row["code"], []):
                    day_concepts[t] += 1
            for concept, _ in day_concepts.most_common(5):
                concept_day_count[concept] += 1

        min_days = min(config.concept_min_days, len(trading_dates))
        persistent = [(c, n) for c, n in concept_day_count.most_common() if n >= min_days]
        return persistent[:10]

    # ------------------------------------------------------------------
    # Step 2: 获取热点概念成分股
    # ------------------------------------------------------------------

    def _load_theme_members(
        self, hot_themes: List[Tuple[str, int]]
    ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, str]]:
        theme_stocks: Dict[str, List[str]] = {}
        stock_themes: Dict[str, List[str]] = {}
        stock_names: Dict[str, str] = {}

        try:
            from data_provider.base import DataFetcherManager
            mgr = DataFetcherManager()
            for theme_name, _ in hot_themes:
                try:
                    members = mgr.get_board_members(theme_name, "concept", 100)
                    codes = []
                    for m in members:
                        code = str(m.get("code", "")).strip()
                        name = str(m.get("name", "")).strip()
                        if code.startswith(("300", "301", "688", "4", "8")):
                            continue
                        codes.append(code)
                        stock_themes.setdefault(code, []).append(theme_name)
                        stock_names[code] = name
                    theme_stocks[theme_name] = codes
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[PatternScreen] 获取概念成分股失败: %s", exc)

        return theme_stocks, stock_themes, stock_names

    # ------------------------------------------------------------------
    # Step 3: 评分筛选
    # ------------------------------------------------------------------

    def _score_and_filter(
        self,
        df: pd.DataFrame,
        stock_themes: Dict[str, List[str]],
        stock_names: Dict[str, str],
        config: PatternScreenerConfig,
    ) -> List[PatternCandidate]:
        candidates = []
        for code, themes in stock_themes.items():
            g = df[df["code"] == code].sort_values("date").reset_index(drop=True)
            if len(g) < 10:
                continue

            close = g["close"].astype(float)
            pct = g["pct_chg"].astype(float)
            volume = g["volume"].astype(float)
            cur = float(close.iloc[-1])

            if cur < config.min_price or cur > config.max_price:
                continue

            ma5 = float(close.rolling(5).mean().iloc[-1])
            ma10 = float(close.rolling(10).mean().iloc[-1])

            avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
            vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0

            if vol_ratio < config.min_vol_ratio:
                continue

            ret3 = float(sum(pct.iloc[-3:])) if len(pct) >= 3 else 0
            ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0

            if ret5 > config.max_ret5:
                continue

            up5 = int(sum(1 for p in pct.iloc[-5:] if p > 0)) if len(pct) >= 5 else 0
            dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0

            consec = 0
            for p in reversed(pct.tolist()):
                if p > 0:
                    consec += 1
                else:
                    break

            score = 0.0
            score += len(themes) * 10
            score += min(vol_ratio, 3.0) * 10
            score += up5 * 5
            if 0 <= dist_ma10 <= 5:
                score += 10
            if ret5 > 10:
                score -= 5

            name = stock_names.get(code, self._get_stock_name(code))

            candidates.append(PatternCandidate(
                code=code, name=name, close=round(cur, 2),
                vol_ratio=round(vol_ratio, 2),
                ret3=round(ret3, 2), ret5=round(ret5, 2),
                up_days_5=up5, dist_ma10=round(dist_ma10, 2),
                consecutive_up=consec, themes=list(themes),
                score=round(score, 1),
            ))

        candidates.sort(key=lambda c: (-c.score, -c.vol_ratio))
        candidates = candidates[:config.max_candidates]

        # 洗盘评分
        self._apply_wash_score(candidates, df)

        return candidates

    # ------------------------------------------------------------------
    # 单股评分（不受过滤条件限制）
    # ------------------------------------------------------------------

    def score_single(self, code: str, date_key: str = "") -> Dict[str, Any]:
        """对指定股票计算规律分+洗盘分，不受初筛过滤限制。

        Args:
            code: 股票代码，如 '002436'
            date_key: 日期，如 '20260609'，空则取最新交易日

        Returns:
            包含规律分明细、洗盘分、v3最终分的字典
        """
        if not date_key:
            date_key = self._get_latest_trading_date() or ""
        if not date_key:
            return {"error": "无法确定交易日期"}

        # 加载数据
        df = self.db.get_bulk_daily_data(days=30)
        g = df[df["code"] == code].sort_values("date").reset_index(drop=True)

        if len(g) < 10:
            return {"error": f"K线数据不足({len(g)}条)"}

        name = self._get_stock_name(code)
        close_s = g["close"].astype(float)
        pct = g["pct_chg"].astype(float)
        volume = g["volume"].astype(float)
        cur = float(close_s.iloc[-1])

        ma5 = float(close_s.rolling(5).mean().iloc[-1])
        ma10 = float(close_s.rolling(10).mean().iloc[-1])
        avg_vol5 = float(volume.rolling(5).mean().iloc[-2]) if len(volume) >= 6 else 0
        vol_ratio = float(volume.iloc[-1]) / avg_vol5 if avg_vol5 > 0 else 0

        ret3 = float(sum(pct.iloc[-3:])) if len(pct) >= 3 else 0
        ret5 = float(sum(pct.iloc[-5:])) if len(pct) >= 5 else 0
        up5 = int(sum(1 for p in pct.iloc[-5:] if p > 0)) if len(pct) >= 5 else 0
        dist_ma10 = (cur / ma10 - 1) * 100 if ma10 > 0 else 0

        consec = 0
        for p in reversed(pct.tolist()):
            if p > 0:
                consec += 1
            else:
                break

        # 热点概念匹配
        stock_themes = []
        concept_cache = self._concept_cache_path(date_key)
        if os.path.exists(concept_cache):
            with open(concept_cache, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            hot_names = set(cdata.get("hot_concepts", {}).keys())
            for concept, stock_list in cdata.get("hot_concepts", {}).items():
                if code in stock_list:
                    stock_themes.append(concept)

        # 规律评分
        score = 0.0
        s1 = len(stock_themes) * 10; score += s1
        s2 = min(vol_ratio, 3.0) * 10; score += s2
        s3 = up5 * 5; score += s3
        s4 = 10 if 0 <= dist_ma10 <= 5 else 0; score += s4
        s5 = -5 if ret5 > 10 else 0; score += s5

        # 洗盘评分
        wash, wash_detail = (0, "")
        if len(g) >= 20:
            wash, wash_detail = self._compute_wash_score(g)

        # v3最终分
        def _v3(s, w):
            if w == 0: return None
            if s > 60: return s + w
            elif s > 50: return s - w if w >= 10 else None
            else: return s + w if w >= 10 else None

        final = _v3(score, wash)

        return {
            "code": code, "name": name, "date": date_key,
            "close": round(cur, 2), "vol_ratio": round(vol_ratio, 2),
            "ret3": round(ret3, 2), "ret5": round(ret5, 2),
            "up_days_5": up5, "dist_ma10": round(dist_ma10, 2),
            "consecutive_up": consec, "themes": stock_themes,
            "score_breakdown": {
                "hot_concepts": s1,
                "vol_ratio": round(s2, 1),
                "up_days": s3,
                "near_ma10": s4,
                "chase_penalty": s5,
            },
            "score": round(score, 1),
            "wash_score": wash, "wash_detail": wash_detail,
            "v3_final": round(final, 1) if final is not None else None,
        }

    # ------------------------------------------------------------------
    # 洗盘评分
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_wash_score(g: pd.DataFrame) -> Tuple[int, str]:
        """计算最近一个交易日的洗盘评分。

        洗盘 = 主力在拉升途中故意打压股价清洗浮筹。
        日线识别依据：
          1. 下影线/实体比（盘中打压后收回）
          2. 收盘位置（收在当日偏高位置）
          3. 多头排列（MA5>MA10>MA20）
          4. 守住MA20（最低价未破中期支撑）
          5. 缩量（主力未出逃）
          6. 贴近MA10（支撑确认）
          7. 前期上涨趋势
        """
        if len(g) < 20:
            return 0, ""

        last = len(g) - 1
        o = float(g.loc[last, "open"])
        h = float(g.loc[last, "high"])
        l = float(g.loc[last, "low"])
        c = float(g.loc[last, "close"])
        prev_c = float(g.loc[last - 1, "close"])
        total_range = h - l

        if total_range <= 0:
            return 0, ""

        # 均线
        ma5 = g["close"].rolling(5).mean()
        ma10 = g["close"].rolling(10).mean()
        ma20 = g["close"].rolling(20).mean()
        if pd.isna(ma20.iloc[last]):
            return 0, ""

        vol_ma5 = g["volume"].rolling(5).mean()
        avg_vol = vol_ma5.iloc[last - 1] if last >= 1 else 0
        vol_ratio = float(g.loc[last, "volume"]) / avg_vol if avg_vol > 0 else 1.0

        # 日内形态
        lower_shadow = min(o, c) - l
        body = abs(c - o)
        ls_body_ratio = lower_shadow / body if body > 0 else 0
        close_pos = (c - l) / total_range

        # 均线状态
        _ma5 = float(ma5.iloc[last])
        _ma10 = float(ma10.iloc[last])
        _ma20 = float(ma20.iloc[last])
        is_bull = _ma5 > _ma10 > _ma20
        hold_ma20 = l > _ma20
        dist_ma10 = (c - _ma10) / _ma10 * 100 if _ma10 > 0 else 0

        # 前5日涨幅
        if last >= 5:
            prev5 = (float(g.loc[last, "close"]) / float(g.loc[last - 5, "close"]) - 1) * 100
        else:
            prev5 = 0

        # 评分
        wash = 0
        tags = []

        # 1. 下影线/实体比（0-4分）
        if ls_body_ratio > 3:
            wash += 4; tags.append("长下影3x")
        elif ls_body_ratio > 2:
            wash += 3; tags.append("长下影2x")
        elif ls_body_ratio > 1:
            wash += 2; tags.append("下影1x")
        elif ls_body_ratio > 0.5:
            wash += 1

        # 2. 收盘位置（0-3分）
        if close_pos > 0.8:
            wash += 3; tags.append("收高位")
        elif close_pos > 0.7:
            wash += 2
        elif close_pos > 0.6:
            wash += 1

        # 3. 多头排列（0-3分）
        if is_bull:
            wash += 3; tags.append("多头")

        # 4. 守住MA20（0-2分）
        if hold_ma20:
            wash += 2; tags.append("守MA20")

        # 5. 缩量（0-2分）
        if vol_ratio < 0.6:
            wash += 2; tags.append("缩量")
        elif vol_ratio < 0.8:
            wash += 1; tags.append("微缩量")

        # 6. 贴近MA10（0-2分）
        if 0 <= dist_ma10 <= 3:
            wash += 2; tags.append("贴MA10")
        elif dist_ma10 > 3:
            wash += 1

        # 7. 前期涨势（0-1分）
        if prev5 > 5:
            wash += 1

        detail = ",".join(tags) if tags else ""
        return wash, detail

    def _apply_wash_score(self, candidates: List[PatternCandidate],
                          daily_df: pd.DataFrame) -> None:
        """批量计算洗盘评分并回写候选。需要至少25天数据来计算MA20。"""
        if not candidates:
            return
        codes = [c.code for c in candidates]
        df_wash = self.db.get_bulk_daily_data(days=30)
        for c in candidates:
            g = df_wash[df_wash["code"] == c.code].sort_values("date").reset_index(drop=True)
            if len(g) < 20:
                continue
            wash, detail = self._compute_wash_score(g)
            c.wash_score = wash
            c.wash_detail = detail

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_basic_metrics(df: pd.DataFrame) -> pd.DataFrame:
        results = []
        for code, group in df.groupby("code"):
            if code.startswith(("300", "301", "688", "4", "8")):
                continue
            g = group.sort_values("date").copy()
            if len(g) < 5:
                continue
            results.append(g)
        if not results:
            return pd.DataFrame()
        return pd.concat(results, ignore_index=True)

    def _load_theme_universe(self, top_n: int, date_key: str = "") -> Dict[str, List[str]]:
        if self._theme_universe_provider:
            return dict(self._theme_universe_provider())

        # 尝试读取缓存（优先指定日期，其次最新）
        cached = self._load_concept_cache(date_key)
        if cached:
            return cached

    def cache_todays_concepts(self) -> Optional[Dict[str, List[str]]]:
        """独立缓存当天的概念板块数据（不需要日线数据，收盘后即可调用）。"""
        try:
            from data_provider.base import DataFetcherManager
            from datetime import datetime
            mgr = DataFetcherManager()
            universe = mgr.get_hot_theme_universe(n=self._default_config().concept_top_n,
                                                    max_members_per_theme=100)
            result = dict(universe) if universe else {}
            if result:
                today_key = datetime.now().strftime("%Y%m%d")
                self._save_concept_cache(today_key, result)
                logger.info("[PatternScreen] 概念数据已缓存: %s (%d只股票)", today_key, len(result))
            return result if result else None
        except Exception as exc:
            logger.warning("[PatternScreen] 缓存概念数据失败: %s", exc)
            return None

    @staticmethod
    def _default_config() -> "PatternScreenerConfig":
        return PatternScreenerConfig()

        # 缓存未命中，从API获取
        try:
            from data_provider.base import DataFetcherManager
            from datetime import datetime
            universe = DataFetcherManager().get_hot_theme_universe(n=top_n, max_members_per_theme=100)
            result = dict(universe) if universe else {}
            if result:
                # 用今天的日期作key（概念数据是当天的实时数据）
                today_key = datetime.now().strftime("%Y%m%d")
                self._save_concept_cache(today_key, result)
            return result
        except Exception as exc:
            logger.warning("[PatternScreen] 获取概念板块失败: %s", exc)
        return {}

    # ------------------------------------------------------------------
    # 概念板块缓存
    # ------------------------------------------------------------------

    @staticmethod
    def _concept_cache_path(date_key: str) -> str:
        return os.path.join(CONCEPT_CACHE_DIR, f"concept_universe_{date_key}.json")

    def _load_concept_cache(self, date_key: str = "") -> Optional[Dict[str, List[str]]]:
        """加载概念板块缓存。优先指定日期，否则取最新缓存。"""
        # 指定日期
        if date_key:
            path = self._concept_cache_path(date_key)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    logger.info("[PatternScreen] 命中概念缓存: %s (%d只股票)",
                                date_key, len(data))
                    return data
                except Exception as exc:
                    logger.warning("[PatternScreen] 概念缓存读取失败: %s", exc)

        # 未指定日期，取最新缓存
        if os.path.isdir(CONCEPT_CACHE_DIR):
            files = [f for f in os.listdir(CONCEPT_CACHE_DIR)
                     if f.startswith("concept_universe_") and f.endswith(".json")]
            if files:
                latest = sorted(files, reverse=True)[0]
                try:
                    with open(os.path.join(CONCEPT_CACHE_DIR, latest), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cache_date = latest.replace("concept_universe_", "").replace(".json", "")
                    logger.info("[PatternScreen] 使用最新概念缓存: %s (%d只股票)",
                                cache_date, len(data))
                    return data
                except Exception as exc:
                    logger.warning("[PatternScreen] 概念缓存读取失败: %s", exc)

        return None

    def _save_concept_cache(self, date_key: str, universe: Dict[str, List[str]]) -> None:
        """保存概念板块映射缓存。"""
        os.makedirs(CONCEPT_CACHE_DIR, exist_ok=True)
        path = self._concept_cache_path(date_key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(universe, f, ensure_ascii=False)
            logger.info("[PatternScreen] 概念缓存已保存: %s (%d只股票)", path, len(universe))
        except Exception as exc:
            logger.warning("[PatternScreen] 概念缓存写入失败: %s", exc)

    def _get_stock_name(self, code: str) -> str:
        if self._stock_name_provider:
            return self._stock_name_provider(code)
        try:
            from data_provider.base import DataFetcherManager
            return DataFetcherManager().get_stock_name(code) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_report(candidates: List[PatternCandidate],
                      hot_themes: List[Tuple[str, int]],
                      date_key: str = "") -> str:
        lines: List[str] = []
        display = ""
        if date_key and len(date_key) == 8:
            display = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"

        lines.append("=" * 90)
        lines.append("  规律选股报告（基于涨跌基因分析）")
        if display:
            lines.append(f"  数据日期: {display}")
        lines.append("=" * 90)

        if hot_themes:
            lines.append("")
            lines.append("  持续热点概念:")
            for name, days in hot_themes[:8]:
                lines.append(f"    {name:<12} 连续 {days}/5 天出现在涨幅 TOP5")
            lines.append("")

        if candidates:
            lines.append(f"  候选股票: {len(candidates)} 只")
            lines.append("")
            lines.append(
                f"  {'代码':<8} {'名称':<8} {'收盘':>7} {'量比':>5} "
                f"{'3日涨':>7} {'5日涨':>7} {'涨/5天':>5} "
                f"{'距MA10':>7} {'连涨':>4} {'评分':>5} {'洗盘':>4}  所属概念"
            )
            lines.append("  " + "-" * 120)

            for c in candidates:
                themes_str = ",".join(c.themes[:3])
                wash_str = str(c.wash_score) if c.wash_score > 0 else "-"
                lines.append(
                    f"  {c.code:<8} {c.name:<8} {c.close:>7.2f} {c.vol_ratio:>5.2f} "
                    f"{c.ret3:>+6.2f}% {c.ret5:>+6.02f}% {c.up_days_5:>3}/5 "
                    f"{c.dist_ma10:>+6.02f}% {c.consecutive_up:>3}天 {c.score:>5.1f} {wash_str:>4}  {themes_str}"
                )
        else:
            lines.append("  无符合条件的股票")

        lines.append("")
        lines.append("  评分: 热点数x10 + 量比x10 + 上涨天数x5 + 贴近MA10加分 - 追高惩罚")
        lines.append("=" * 90)
        return "\n".join(lines)
