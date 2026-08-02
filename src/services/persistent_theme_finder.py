# -*- coding: utf-8 -*-
"""持续热点题材查找器 — 唯一权威实现，强制走 stock_concept_membership 数据库。

设计目标：
- 所有需要"持续热点"概念的策略（PatternScreener / SurgeScreener / WashBacktester 等）
  统一通过本模块获取，保证算法一致、数据源一致。
- 永不依赖 concept_universe_*.json（已废弃 --cache-concepts 路径）。

两类查询：
- find()：返回 T 日所有持续热点（按命中天数降序）
- find_emerging_themes()：返回 T 日相对 T-1 日"新增"的持续热点（T 有 T-1 无）
  → 老热点已被市场消化，真正机会在今日新冒头的题材

基础算法（find）：
1. 取最近 N 个交易日（默认 15）全市场日线，算 pct_chg。
2. 取最近 5 个交易日，每天统计涨幅 ≥3% 个股的题材（按命中次数取当日 top5）。
   - 同步剔除 NON_THEME_KEYWORDS 命中的非主题概念（融资融券/沪股通/MSCI/
     ST/创业板/股权转让/一带一路/国企改革 等"全包概念"），避免霸榜持续热点。
3. 统计每个题材的"命中天数"，按天数降序返回前 10。
   - 默认 concept_min_days=1：所有命中过的题材都返回，调用方按 days 字段自判持续性
   - 传 concept_min_days=3 还原"≥3 天强持续"严格口径
4. 天数本身即持续性信号，不再强制硬阈值。

新增算法（find_emerging_themes）：
- find(T) - find(T-1) 集合差，按 T 日命中天数降序
"""


from __future__ import annotations

import logging
from collections import Counter
from typing import Dict, List, Tuple

from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


# 非主题类概念关键词黑名单
# 这些"概念"按财务/通道/上市属性/资本运作/泛政策归类，不是真行业主题，
# 但因成分股数量大（多被 API 截断在 200）容易长期霸榜"持续热点"，
# 故在 find() 统计 top5 题材时直接剔除。
NON_THEME_KEYWORDS: set = {
    # 财务因子（按业绩/价格归类，与行业主题无关）
    "预盈预增", "预亏预减", "预增", "预减", "商誉", "高送转", "高股息", "高分红",
    "高价股", "低价股", "券商金股", "业绩预增", "业绩预减",
    # 通道/指数标签（按交易通道/指数成分归类）
    "沪股通", "深股通", "融资融券", "MSCI", "富时罗素", "上证", "沪深", "中证",
    "标普", "纳斯达克",
    # 上市属性（按板块/新股归类）
    "ST板块", "ST个股", "ST", "注册制", "科创板", "创业板", "北交所",
    "次新股", "新股", "老股",
    # 资本运作（按公司行为归类）
    "股权转让", "并购重组", "借壳", "定向增发", "回购", "增减持", "分拆",
    # 泛政策/地理概念（过宽，相当于全市场）
    "西部大开发", "粤港澳大湾区", "京津冀", "长三角", "一带一路", "乡村振兴",
    "海峡两岸", "长江经济带", "央企国企", "国企改革", "国资改革",
    "国家底部", "国家大基金", "自贸区",
    # 风格因子
    "蓝筹", "白马", "绩优股", "价值股", "周期股", "抗通胀", "高成长",
    # 事件型因子
    "人民币贬值", "含H股", "含B股", "转融券", "创投",
}


def is_theme_concept(name: str) -> bool:
    """检测题材名称是否为真主题概念（非 ETF 类、非财务因子、非通道标签、非泛政策）。

    Args:
        name: 题材名称，如 "国企改革" / "AI应用" / "CPO"

    Returns:
        True 表示是真主题概念，应参与持续热点统计；
        False 表示是全包概念/财务因子/通道标签等，应剔除。
    """
    if not name:
        return False
    return not any(kw in name for kw in NON_THEME_KEYWORDS)


class PersistentThemeFinder:
    """持续热点题材查找器（强制 DB 数据源，永不读 JSON）。

    可选注入 `theme_universe_provider`（如盘中场景传入实时 API universe），
    注入后 find() 优先使用 provider 数据；不注入则走 stock_concept_membership DB。
    永不读 concept_universe_*.json。
    """

    def __init__(
        self,
        db: "DatabaseManager | None" = None,
        theme_universe_provider: "callable | None" = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self._theme_universe_provider = theme_universe_provider
        # universe 缓存：避免同一实例在同一次调用链里反复扫 40331 条 DB 记录
        # 生命周期 = 实例本身；provider 模式不缓存（实时数据可能变化）
        self._cached_universe: "Dict[str, List[str]] | None" = None

    # ------------------------------------------------------------------
    # 持续热点主算法
    # ------------------------------------------------------------------

    def find(
        self,
        *,
        lookback_days: int = 15,
        concept_min_days: int = 1,
        top_n_per_day: int = 5,
        min_pct_chg: float = 3.0,
        date_key: str = "",
    ) -> List[Tuple[str, int]]:
        """返回持续热点题材列表 [(theme_name, days), ...]，最多 10 个。

        按"命中天数"降序返回，天数越多持续性越强；不再强制硬阈值，调用方按需
        根据 days 字段判断是否采纳（如 days>=3 视为强持续，days==1 视为偶发）。

        Args:
            lookback_days: 加载最近 N 个交易日全市场日线（默认 15）
            concept_min_days: 题材至少 N 天进涨幅 top5 才返回（默认 1 = 不过滤，
                按天数降序全部返回前 10；传 3 还原"≥3 天强持续"严格口径）
            top_n_per_day: 每天取涨幅 ≥ min_pct_chg 个股命中最多的前 N 个题材（默认 5）
            min_pct_chg: 当日涨幅阈值（默认 3.0%）
            date_key: YYYYMMDD 截止日期；"" 取数据库最新交易日
        """
        df = self.db.get_bulk_daily_data(days=lookback_days, end_date=date_key or None)
        if df.empty:
            logger.warning(
                "[PersistentTheme] 无日线数据 (lookback=%d, end=%s)",
                lookback_days, date_key or "最新",
            )
            return []

        # 算 pct_chg（与 PatternScreener._compute_basic_metrics 一致）
        df = df.sort_values(["code", "date"])
        df["prev_close"] = df.groupby("code")["close"].shift(1)
        df["pct_chg"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100
        df = df.dropna(subset=["pct_chg"])
        if df.empty:
            return []

        trading_dates = sorted(df["date"].unique(), reverse=True)[:5]

        # 数据源：优先用 provider（盘中实时 API 注入），否则走 stock_concept_membership DB
        # 通过 _get_theme_universe() 复用缓存，避免反复扫 DB
        theme_universe = self._get_theme_universe()
        if self._theme_universe_provider:
            logger.info("[PersistentTheme] 使用注入的 universe: %d 只股票", len(theme_universe))
        if not theme_universe:
            logger.warning(
                "[PersistentTheme] 概念池为空（%s），"
                "请先运行 --sync-concept-membership 同步概念成分股",
                "provider 注入" if self._theme_universe_provider else "stock_concept_membership",
            )
            return []

        concept_day_count: Counter = Counter()
        for d in trading_dates:
            day_df = df[(df["date"] == d) & (df["pct_chg"] >= min_pct_chg)]
            day_concepts: Counter = Counter()
            for _, row in day_df.iterrows():
                for t in theme_universe.get(str(row["code"]), []):
                    # 跳过非主题类概念（融资融券/沪股通/MSCI/ST/创业板/股权转让/一带一路/国企改革 等）
                    if not is_theme_concept(t):
                        continue
                    day_concepts[t] += 1
            for concept, _ in day_concepts.most_common(top_n_per_day):
                concept_day_count[concept] += 1

        min_days = min(concept_min_days, len(trading_dates))
        persistent = [(c, n) for c, n in concept_day_count.most_common() if n >= min_days]
        result = persistent[:10]
        # DEBUG 级别：find() 是基础方法，emerging_themes 内部会调它两次（T 和 T-1），
        # 中间结果的 INFO 日志会误导用户以为最终用的是 T 全集；真正面向用户的是
        # emerging_themes 的"T 新增 X 个"日志（INFO 级别）
        logger.debug(
            "[PersistentTheme] find() date=%s 持续热点(全集) %d 个: %s",
            date_key or "最新", len(result), [t for t, _ in result],
        )
        return result

    # ------------------------------------------------------------------
    # 今日新增持续热点（T − T-1 集合差）
    # ------------------------------------------------------------------

    def find_emerging_themes(
        self,
        *,
        lookback_days: int = 15,
        concept_min_days: int = 1,
        top_n_per_day: int = 5,
        min_pct_chg: float = 3.0,
        date_key: str = "",
    ) -> List[Tuple[str, int]]:
        """T 日相对 T-1 日新增的持续热点（T 有 T-1 无）。

        底层逻辑：老热点（T-1 已发酵）机会已被市场消化；
        真正的交易价值在"今日新冒头"的热点（T 有 T-1 无）。

        算法：
        1. find(date=T) → T 日持续热点集合
        2. find(date=T-1) → T-1 日持续热点集合
        3. 集合差：T - T-1 = 新增热点
        4. 按 T 日命中天数降序

        Args:
            参数定义同 find()
            date_key: T 日；"" 取 DB 最新交易日

        Returns:
            List[Tuple[str, int]]: T 日新增持续热点 [(theme, days_in_T), ...]，最多 10 个
        """
        t_themes = self.find(
            lookback_days=lookback_days,
            concept_min_days=concept_min_days,
            top_n_per_day=top_n_per_day,
            min_pct_chg=min_pct_chg,
            date_key=date_key,
        )

        t_minus_1 = self._get_previous_trading_date(date_key)
        if not t_minus_1:
            logger.warning(
                "[PersistentTheme] 找不到 T-1 交易日 (T=%s)，返回 T 日全部持续热点",
                date_key or "最新",
            )
            return t_themes

        t_minus_1_themes = self.find(
            lookback_days=lookback_days,
            concept_min_days=concept_min_days,
            top_n_per_day=top_n_per_day,
            min_pct_chg=min_pct_chg,
            date_key=t_minus_1,
        )

        t_minus_1_set = {t for t, _ in t_minus_1_themes}
        # 集合差：T 有 T-1 无；保留 T 的命中天数
        emerging = [(t, d) for t, d in t_themes if t not in t_minus_1_set]
        # 按 T 日命中天数降序（find 已降序，集合差后仍保序）

        logger.info(
            "[PersistentTheme] T=%s T-1=%s | T=%d T-1=%d 新增=%d: %s",
            date_key or "最新", t_minus_1,
            len(t_themes), len(t_minus_1_themes), len(emerging),
            [t for t, _ in emerging],
        )
        return emerging

    def _get_previous_trading_date(self, date_key: str) -> "str | None":
        """从 DB 取 T 的上一交易日（不依赖日历，按 stock_daily 实际数据）。

        Args:
            date_key: 'YYYYMMDD' 或空字符串；空 → 取 DB 最新交易日的上一日

        Returns:
            'YYYYMMDD' 字符串，或 None（DB 数据不足）
        """
        from sqlalchemy import text as sa_text

        with self.db.get_session() as session:
            if date_key:
                s = str(date_key).replace("-", "")
                if len(s) == 8:
                    iso = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                else:
                    iso = str(date_key)
                r = session.execute(
                    sa_text("SELECT MAX(date) FROM stock_daily WHERE date < :d"),
                    {"d": iso},
                ).fetchone()
            else:
                # date_key 为空 → 取倒数第二个交易日（最新的上一日）
                r = session.execute(sa_text(
                    "SELECT date FROM stock_daily ORDER BY date DESC LIMIT 1 OFFSET 1"
                )).fetchone()

        if not r or not r[0]:
            return None
        return str(r[0]).replace("-", "")

    # ------------------------------------------------------------------
    # 题材池加载（强制 DB）
    # ------------------------------------------------------------------

    def _get_theme_universe(self) -> Dict[str, List[str]]:
        """获取题材池（带缓存）。

        - 注入 provider 模式（盘中实时 API）：不缓存，每次取最新（盘中 universe 会变）
        - DB 模式：缓存在 self._cached_universe，同一实例后续调用零开销复用

        一次 _load_hot_concept_codes 调用链里（find + 反查成分股）可省掉一次扫 40331 条 DB。
        """
        if self._theme_universe_provider:
            return dict(self._theme_universe_provider())
        if self._cached_universe is None:
            self._cached_universe = self.load_themes_from_db()
        return self._cached_universe

    @property
    def theme_universe(self) -> Dict[str, List[str]]:
        """暴露缓存的 universe 供外部复用（避免重复扫 DB）。

        - 注入 provider 模式：直接转发 provider（不缓存）
        - DB 模式：返回 self._cached_universe，若未缓存则触发一次加载
        """
        return self._get_theme_universe()

    @staticmethod
    def load_themes_from_db() -> Dict[str, List[str]]:
        """从 stock_concept_membership 表读取全市场题材池。

        Returns:
            {code: [concept_name, ...]} 全市场股票→题材反向映射
        """
        from sqlalchemy import text

        db = DatabaseManager()
        with db.get_session() as session:
            rows = session.execute(
                text("SELECT code, concept_name FROM stock_concept_membership")
            ).fetchall()

        universe: Dict[str, List[str]] = {}
        for code, name in rows:
            if not code or not name:
                continue
            code_str = str(code)
            universe.setdefault(code_str, [])
            if name not in universe[code_str]:
                universe[code_str].append(name)

        logger.info(
            "[PersistentTheme] 从 DB 加载概念池: %d 只股票, %d 条归属关系",
            len(universe), len(rows),
        )
        return universe
