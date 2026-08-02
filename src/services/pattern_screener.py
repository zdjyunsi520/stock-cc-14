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
from tabulate import tabulate

from src.storage import DatabaseManager
from src.utils.stock_filter import is_excluded_board

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                         "data", "pattern_cache")


@dataclass
class PatternScreenerConfig:
    min_price: float = 0.0            # 最低价格
    max_price: float = 99999.0        # 最高价格
    min_vol_ratio: float = 0.8        # 最低量比
    max_ret5: float = 20.0            # 5日涨幅上限（排除追高）
    concept_top_n: int = 20           # 取前N个热点概念
    concept_min_days: int = 1         # 持续热点最少命中天数（1=按天数降序全返回，3=仅强持续）
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
    theme_score: float = 0.0     # 概念分（热点数×10），从规律分拆出单独展示
    wash_score: int = 0          # 洗盘评分（0-20）
    wash_detail: str = ""        # 洗盘特征摘要

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PatternCandidate":
        # 旧缓存兼容：theme_score 缺失或为 0 时，按 themes 长度推算（热点数×10）
        if not d.get("theme_score"):
            themes = d.get("themes") or []
            d = {**d, "theme_score": float(len(themes) * 10)}
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

        # Step 2: 获取热点概念成分股（优先用 concept_cache 反向构造，避免调 akshare）
        theme_universe = self._load_theme_universe(config.concept_top_n, date_key=date_key)
        theme_stocks, stock_themes, stock_names = self._load_theme_members(
            hot_themes, theme_universe=theme_universe
        )

        # Step 3: 加载日线 + 指标计算 + 筛选评分
        # 关键：传入 end_date=date_key，确保历史日期回测只用到该日期之前的数据
        df = self.db.get_bulk_daily_data(days=config.lookback_days, end_date=date_key)
        if df.empty:
            return [], hot_themes

        candidates = self._score_and_filter(df, stock_themes, stock_names, config, date_key=date_key)

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
        """持续热点题材（已委托给 PersistentThemeFinder）。

        保留方法签名以兼容现有调用方（main.py / intraday_persistent_themes 等），
        内部转调 PersistentThemeFinder.find_emerging_themes（T − T-1 集合差），
        让所有走 PatternScreener 的场景（wash 选股/wash-backtest/盘中/缩池等）
        自动用最新算法，无需改调用入口。
        数据源：优先用 self._theme_universe_provider（盘中实时 API），否则走 DB。
        永不走 JSON。
        """
        from src.services.persistent_theme_finder import PersistentThemeFinder
        return PersistentThemeFinder(
            db=self.db,
            theme_universe_provider=self._theme_universe_provider,
        ).find_emerging_themes(
            lookback_days=config.lookback_days,
            concept_min_days=config.concept_min_days,
            date_key=date_key,
        )

    # ------------------------------------------------------------------
    # Step 2: 获取热点概念成分股
    # ------------------------------------------------------------------

    def _load_theme_members(
        self, hot_themes: List[Tuple[str, int]],
        theme_universe: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, str]]:
        """获取热点题材的成分股。

        优先用 theme_universe（concept_cache 的 {code: [题材]} 反向结构）构造，
        避免每次调 akshare 拉成员导致结果不稳定。cache 缺失才 fallback 到 akshare。
        """
        theme_stocks: Dict[str, List[str]] = {}
        stock_themes: Dict[str, List[str]] = {}
        stock_names: Dict[str, str] = {}

        hot_theme_names = {name for name, _ in hot_themes}

        # 路径1：从 concept_cache 反向构造（稳定，不调 API）
        if theme_universe:
            theme_to_codes: Dict[str, List[str]] = {}
            for code, themes in theme_universe.items():
                for t in themes:
                    if t in hot_theme_names:
                        theme_to_codes.setdefault(t, []).append(str(code))

            for theme_name in hot_theme_names:
                codes = theme_to_codes.get(theme_name, [])
                valid = [c for c in codes if not is_excluded_board(c)]
                theme_stocks[theme_name] = valid
                for c in valid:
                    stock_themes.setdefault(c, []).append(theme_name)
                    if c not in stock_names:
                        stock_names[c] = self._get_stock_name(c)
            logger.info("[PatternScreen] 题材成员来自 concept_cache（共%d题材）",
                        len(theme_stocks))
            return theme_stocks, stock_themes, stock_names

        # 路径2：fallback 调 akshare（不稳定，保留以兼容老调用方）
        try:
            from data_provider.base import get_data_fetcher_manager
            mgr = get_data_fetcher_manager()
            for theme_name, _ in hot_themes:
                try:
                    members = mgr.get_board_members(theme_name, "concept", 100)
                    codes = []
                    for m in members:
                        code = str(m.get("code", "")).strip()
                        name = str(m.get("name", "")).strip()
                        if is_excluded_board(code):
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
        date_key: str = "",
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
            theme_score = len(themes) * 10          # 概念分（热点数×10），单独存
            score += theme_score
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
                theme_score=float(theme_score),
            ))

        candidates.sort(key=lambda c: (-c.score, -c.vol_ratio))
        candidates = candidates[:config.max_candidates]

        # 洗盘评分
        self._apply_wash_score(candidates, df, date_key=date_key)

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

        # 加载数据（按 date_key 截止，保证历史回测复现）
        df = self.db.get_bulk_daily_data(days=30, end_date=date_key or None)
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

        # 热点概念匹配（修复：原代码误读 hot_concepts 字段，实际文件是 {code: [themes]} 反向结构）
        stock_themes = []
        try:
            hot_themes = self._find_persistent_themes(PatternScreenerConfig(), date_key=date_key)
            hot_names = {name for name, _ in hot_themes}
            if hot_names:
                universe = self._load_theme_universe(PatternScreenerConfig().concept_top_n, date_key=date_key)
                code_themes = universe.get(code, []) or universe.get(str(code), [])
                stock_themes = [t for t in code_themes if t in hot_names]
        except Exception as exc:
            logger.debug("[score_single] 热点概念匹配失败 %s: %s", code, exc)

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
    # 实时洗盘评分（日线 + 分时线）
    # ------------------------------------------------------------------

    def score_realtime(self, code: str) -> Dict[str, Any]:
        """结合日线 + 当日分时线的实时洗盘评分。

        Returns:
            包含日线洗盘分、分时洗盘分、综合评分的字典。
        """
        # 1. 日线基础评分（复用 score_single）
        base = self.score_single(code)
        if "error" in base:
            return base

        # 2. 拉分时线
        try:
            from src.services.intraday_minute_provider import IntradayMinuteCacheProvider
            provider = IntradayMinuteCacheProvider()
            minutes = provider.get_minutes(code)
        except Exception as exc:
            logger.warning("[RealtimeWash] 分时数据获取失败: %s", exc)
            base["intraday_wash"] = None
            base["intraday_detail"] = f"分时数据获取失败: {exc}"
            return base

        if not minutes:
            base["intraday_wash"] = None
            base["intraday_detail"] = "无分时数据（非交易时段或数据源异常）"
            return base

        # 3. 分时洗盘评分
        i_wash, i_detail = self._compute_intraday_wash(minutes, base)
        base["intraday_wash"] = i_wash
        base["intraday_detail"] = i_detail

        # 4. 综合洗盘分 = 日线洗盘 + 分时洗盘
        daily_wash = base.get("wash_score", 0)
        total_wash = daily_wash + i_wash
        base["total_wash"] = total_wash

        # 5. 用综合洗盘分重算 v3
        s = base.get("score", 0)
        def _v3(s, w):
            if w == 0: return None
            if s > 60: return s + w
            elif s > 50: return s - w if w >= 10 else None
            else: return s + w if w >= 10 else None

        base["v3_final_realtime"] = round(v, 1) if (v := _v3(s, total_wash)) is not None else None

        return base

    @staticmethod
    def _compute_intraday_wash(
        minutes: List[Dict[str, Any]], base: Dict[str, Any]
    ) -> Tuple[int, str]:
        """基于分时线的洗盘评分（0-15分）。

        维度:
          1. V型反转（盘中急跌后收回）  0-4
          2. 量能前重后轻（洗盘量型）   0-3
          3. 价格守均价线               0-3
          4. 尾盘稳健/拉尾              0-3
          5. 分时低点未破前日低点       0-2
        """
        wash = 0
        tags = []

        # 过滤有效数据
        bars = [m for m in minutes if m.get("close") is not None and m.get("volume") is not None]
        if len(bars) < 10:
            return 0, "分时数据不足"

        closes = [float(b["close"]) for b in bars]
        volumes = [float(b["volume"]) for b in bars]
        avg_prices = [float(b["avg_price"]) for b in bars if b.get("avg_price")]

        high = max(closes)
        low = min(closes)
        last = closes[-1]
        total_range = high - low

        # 1. V型反转：盘中最大回撤幅度 vs 收盘位置
        if total_range > 0:
            max_drop = (high - low) / high * 100
            recovery = (last - low) / total_range
            if max_drop > 3 and recovery > 0.8:
                wash += 4; tags.append("V型反转")
            elif max_drop > 2 and recovery > 0.7:
                wash += 3; tags.append("深V")
            elif max_drop > 1 and recovery > 0.6:
                wash += 2; tags.append("浅V")
            elif recovery > 0.7:
                wash += 1; tags.append("偏强")

        # 2. 量能分布：前半段放量 / 后半段缩量 = 洗盘特征
        mid = len(bars) // 2
        if mid > 0:
            vol_first = sum(volumes[:mid]) / mid
            vol_second = sum(volumes[mid:]) / (len(bars) - mid)
            if vol_second > 0:
                vol_ratio = vol_first / vol_second
                if vol_ratio > 2.0:
                    wash += 3; tags.append("前放后缩")
                elif vol_ratio > 1.3:
                    wash += 2; tags.append("量前重")
                elif vol_ratio > 0.9:
                    wash += 1

        # 3. 价格 vs 均价线
        if avg_prices:
            above_count = sum(1 for c, a in zip(closes, avg_prices) if c >= a)
            above_ratio = above_count / len(avg_prices)
            if above_ratio >= 0.8:
                wash += 3; tags.append("守均价")
            elif above_ratio >= 0.6:
                wash += 2; tags.append("均价上")
            elif above_ratio >= 0.4:
                wash += 1

        # 4. 尾盘行为（最后30分钟）
        tail_n = min(30, len(bars))
        if tail_n >= 5:
            tail_closes = closes[-tail_n:]
            tail_trend = (tail_closes[-1] - tail_closes[0]) / tail_closes[0] * 100
            if tail_trend > 0.3:
                wash += 3; tags.append("拉尾")
            elif tail_trend > -0.1:
                wash += 2; tags.append("尾盘稳")
            elif tail_trend > -0.3:
                wash += 1

        # 5. 分时低点未破前日收盘价（日线支撑）
        daily_close = base.get("close", 0)
        if daily_close > 0:
            intraday_low = low
            if intraday_low >= daily_close * 0.98:
                wash += 2; tags.append("未破前收")
            elif intraday_low >= daily_close * 0.97:
                wash += 1

        detail = ",".join(tags) if tags else ""
        return wash, detail

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
                          daily_df: pd.DataFrame,
                          date_key: str = "") -> None:
        """批量计算洗盘评分并回写候选。需要至少25天数据来计算MA20。

        Args:
            date_key: 截止日期 YYYYMMDD，保证历史回测时只用到该日期前的数据。
        """
        if not candidates:
            return
        codes = [c.code for c in candidates]
        # 按 date_key 截止取数（修复历史回测数据穿越 bug）
        df_wash = self.db.get_bulk_daily_data(days=30, end_date=date_key or None)
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
            if is_excluded_board(code):
                continue
            g = group.sort_values("date").copy()
            if len(g) < 5:
                continue
            results.append(g)
        if not results:
            return pd.DataFrame()
        return pd.concat(results, ignore_index=True)

    def _load_theme_universe(self, top_n: int, date_key: str = "") -> Dict[str, List[str]]:
        """题材成分股池（强制 DB 数据源）。

        优先使用外部注入的 `_theme_universe_provider`（main.py 加载后注入，避免重复查 DB），
        否则直接调 PersistentThemeFinder.load_themes_from_db。
        永不读 JSON（已废弃 --cache-concepts 路径）。
        """
        if self._theme_universe_provider:
            return dict(self._theme_universe_provider())

        from src.services.persistent_theme_finder import PersistentThemeFinder
        return PersistentThemeFinder.load_themes_from_db()

    @staticmethod
    def _default_config() -> "PatternScreenerConfig":
        return PatternScreenerConfig()

    def _get_stock_name(self, code: str) -> str:
        if self._stock_name_provider:
            return self._stock_name_provider(code)
        try:
            from data_provider.base import get_data_fetcher_manager
            return get_data_fetcher_manager().get_stock_name(code) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    @staticmethod
    def compute_wash_final(score: float, wash_score: int) -> Optional[float]:
        """洗盘综合分算法（v3）。

        - score>60：综合 = 规律分 + 洗盘分（共振加分）
        - 50<score<=60：洗盘>=10 时综合 = 规律分 - 洗盘分（高洗减分），否则淘汰
        - score<=50：洗盘>=10 时综合 = 规律分 + 洗盘分（强洗加分），否则淘汰
        - wash_score=0：返回 None（不入选）
        """
        if wash_score == 0:
            return None
        if score > 60:
            return score + wash_score
        elif score > 50:
            return score - wash_score if wash_score >= 10 else None
        else:
            return score + wash_score if wash_score >= 10 else None

    @staticmethod
    def format_report(candidates: List[PatternCandidate],
                      hot_themes: List[Tuple[str, int]],
                      date_key: str = "",
                      slim: bool = False) -> str:
        lines: List[str] = []
        display = ""
        if date_key and len(date_key) == 8:
            display = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"

        title = "洗盘选股V3报告" if slim else "规律选股报告（基于涨跌基因分析）"
        lines.append("=" * 90)
        lines.append(f"  {title}")
        if display:
            lines.append(f"  数据日期: {display}")
        lines.append("=" * 90)

        if not slim and hot_themes:
            lines.append("")
            lines.append("  持续热点概念:")
            for name, days in hot_themes[:8]:
                lines.append(f"    {name:<12} 连续 {days}/5 天出现在涨幅 TOP5")
            lines.append("")

        if candidates:
            lines.append(f"  候选股票: {len(candidates)} 只")
            lines.append("")
            if slim:
                headers = ['代码', '名称', '概念', '规律', '洗盘', '综合']
                table_rows = []
                for c in candidates:
                    wash_str = str(c.wash_score) if c.wash_score > 0 else "-"
                    final = PatternScreener.compute_wash_final(c.score, c.wash_score)
                    final_str = f"{final:.1f}" if final is not None else "-"
                    table_rows.append([
                        c.code, c.name, f"{c.theme_score:.0f}",
                        f"{c.score:.1f}", wash_str, final_str,
                    ])
                lines.append(tabulate(
                    table_rows, headers=headers, tablefmt='grid', numalign='right',
                ))
            else:
                headers = ['代码', '名称', '收盘', '量比', '3日涨', '5日涨',
                           '涨/5天', '距MA10', '连涨', '概念', '规律', '洗盘', '综合', '所属概念']
                table_rows = []
                for c in candidates:
                    wash_str = str(c.wash_score) if c.wash_score > 0 else "-"
                    final = PatternScreener.compute_wash_final(c.score, c.wash_score)
                    final_str = f"{final:.1f}" if final is not None else "-"
                    themes_str = ",".join(c.themes[:3])
                    table_rows.append([
                        c.code, c.name, f"{c.close:.2f}", f"{c.vol_ratio:.2f}",
                        f"{c.ret3:+.2f}%", f"{c.ret5:+.2f}%", f"{c.up_days_5}/5",
                        f"{c.dist_ma10:+.2f}%", f"{c.consecutive_up}天",
                        f"{c.theme_score:.0f}", f"{c.score:.1f}", wash_str, final_str, themes_str,
                    ])
                lines.append(tabulate(
                    table_rows, headers=headers, tablefmt='grid', numalign='right',
                ))
        else:
            lines.append("  无符合条件的股票")

        lines.append("")
        if slim:
            lines.append("  综合: v3算法(w=0淘汰 | s>60: s+w | 50<s<=60: s-w需w>=10 | s<=50: s+w需w>=10)")
        else:
            lines.append("  规律: 热点数x10 + 量比x10 + 上涨天数x5 + 贴近MA10加分 - 追高惩罚")
            lines.append("  概念: 热点数×10（已从规律分拆出单独展示）")
            lines.append("  综合: v3算法(>60加洗盘, 50-60高洗减, <=50强洗加)")
        lines.append("=" * 90)
        return "\n".join(lines)
