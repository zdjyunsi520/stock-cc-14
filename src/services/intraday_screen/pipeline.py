# -*- coding: utf-8 -*-
"""盘中选股 pipeline（编排器）。

流程（与 --pattern-screen --wash-v3 等效，无资金流处理）：
1. 时间守卫（9:30 + 工作日）
2. 建 tmp 库（删旧 + 建空表，仅日线）
3. 实时热点概念 + 成分股（akshare，不碰 concept_cache）
4. 拷历史日线到 tmp 库
5. tushare stk_mins 抓 1min 分时 → 聚合 today 写 tmp 库
6. IntradayPatternScreener.screen_intraday()
7. picked → compute_wash_final 筛选 → slim 表格输出
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# 默认配置
DEFAULT_THEME_N = 20   # 与 PatternScreenerConfig.concept_top_n 对齐
DEFAULT_MAX_CANDIDATES = 0  # 0=不限，处理全部获取到的成分股；显式传 N 才截断前 N 只


def run_intraday_screen_with_wash(
    *,
    theme_n: int = DEFAULT_THEME_N,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    db_path: str = "data/intraday_screen.db",
    codes: Optional[List[str]] = None,
) -> Tuple[int, str]:
    """盘中选股主入口（--wash-v3 等效，无资金流）。

    Args:
        theme_n: 取实时涨幅前 N 个概念（codes 模式下不生效）
        max_candidates: 最多处理的成分股数量（避免运行时间过长；codes 模式下不生效）
        db_path: 临时库文件路径
        codes: 指定股票代码列表。非空时跳过实时热点概念获取，直接对这些股票评分。

    Returns:
        (exit_code, report_text)
        exit_code=0 表示成功；非 0 表示守卫拒绝或流程异常
    """
    # === 守卫 1: 交易日 + 9:30 ===
    from src.utils.trading_date import TradingDate

    now = datetime.now()
    today = now.date()
    if not TradingDate.is_trading_day(today):
        msg = f"[IntradayScreen] 拒绝：非交易日（{today.strftime('%Y-%m-%d')} 周末）"
        logger.warning(msg)
        return 1, msg
    if now.time() < dtime(9, 30):
        msg = f"[IntradayScreen] 拒绝：未到 9:30 开盘（当前 {now.strftime('%H:%M')}）"
        logger.warning(msg)
        return 1, msg

    logger.info(
        "[IntradayScreen] 启动盘中选股: %s %s, theme_n=%d, max_candidates=%d",
        today.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
        theme_n, max_candidates,
    )

    # === Step 1: 建临时库（仅日线） ===
    from .intraday_db import build_intraday_db, copy_history_from_production

    engine = build_intraday_db(db_path=db_path)

    # === Step 2: 候选股池（两种模式）===
    if codes:
        # 指定股票池模式：跳过热点概念获取，直接用 codes 评分
        from .intraday_realtime_concepts import _lookup_stock_names
        candidate_codes = list(codes)
        stock_themes = {c: [] for c in candidate_codes}
        stock_names = _lookup_stock_names(candidate_codes)
        hot_themes = []
        logger.info("[IntradayScreen] 指定股票池模式: %d 只 codes", len(candidate_codes))
    else:
        # 实时热点概念成分股模式（与 --cache-concepts 同算法，仅不入 JSON）
        from .intraday_realtime_concepts import get_realtime_theme_universe

        stock_themes, stock_names, hot_themes = get_realtime_theme_universe(theme_n)
        if not stock_themes:
            msg = "[IntradayScreen] 未取到成分股或实时热点概念（akshare 接口可能失败）"
            logger.error(msg)
            return 1, msg

        # 持续热点筛选：与 PatternScreener._find_persistent_themes 完全同算法
        # universe 用实时 API 的 stock_themes（替代盘后的 JSON concept_cache）
        from .intraday_persistent_themes import filter_persistent_hot_themes
        original_size = len(stock_themes)
        persistent_concepts = filter_persistent_hot_themes(stock_themes)
        if persistent_concepts:
            persistent_set = {name for name, _ in persistent_concepts}
            filtered = {
                c: [t for t in themes if t in persistent_set]
                for c, themes in stock_themes.items()
            }
            stock_themes = {c: t for c, t in filtered.items() if t}
            logger.info(
                "[IntradayScreen] 持续热点过滤: TOP%d → 持续%d个, 候选股 %d→%d 只",
                len(hot_themes), len(persistent_concepts),
                original_size, len(stock_themes),
            )
            hot_themes = [(t[0], t[1]) for t in hot_themes if t[0] in persistent_set]
            if not stock_themes:
                msg = "[IntradayScreen] 持续热点过滤后候选股为空（持续热点无成分股命中）"
                logger.warning(msg)
                return 0, msg
        else:
            logger.warning(
                "[IntradayScreen] 持续热点筛选返回空，沿用全部 TOP%d 实时热点",
                len(hot_themes),
            )

        # 限制候选股数（max_candidates=0 表示不限制，处理全部获取到的成分股）
        all_codes = list(stock_themes.keys())
        if max_candidates and max_candidates > 0:
            candidate_codes = all_codes[:max_candidates]
        else:
            candidate_codes = all_codes
        stock_themes = {c: stock_themes[c] for c in candidate_codes}
        stock_names = {c: stock_names.get(c, "") for c in candidate_codes}

    if hot_themes:
        print(
            f"\n[IntradayScreen] 候选股 {len(candidate_codes)} 只，"
            f"持续热点概念: {', '.join(t[0] for t in hot_themes)}"
        )
    else:
        print(f"\n[IntradayScreen] 指定股票池模式: 候选股 {len(candidate_codes)} 只")

    # === Step 3: 拷历史日线到 tmp 库（限定 codes 范围，加速） ===
    # lookback_days=35：评分需要 15 天 + 洗盘 MA20 需要 20+ 天，留余量
    copy_history_from_production(
        engine, codes=candidate_codes, lookback_days=35, end_date=today,
    )

    # === Step 4: 同花顺 single_trend 抓当日分时 → 聚合 today → tmp 库 ===
    from .intraday_minute_aggregator import (
        aggregate_today_from_1min,
        aggregate_trend_to_db,
        fetch_trend,
    )

    # 4a. cookie 预检：用第一只候选股试抓，失败即 cookie/接口失效，直接退出
    pre_code = candidate_codes[0]
    print(f"[IntradayScreen] 预检 cookie：抓 {pre_code} ...")
    try:
        pre_trend = fetch_trend(pre_code)
    except Exception as exc:
        # _resolve_credentials 缺配置时抛 RuntimeError
        msg = "\n".join([
            "=" * 100,
            f"[IntradayScreen] cookie 预检失败：{exc}",
            "",
            "请在项目根目录 .env 文件配置：",
            "  INTRADAY_HX_COOKIE=user=MDpteF9...（cURL -b '...' 里的整段 cookie，注意保留分号和空格）",
            "  INTRADAY_HX_FUYAO_AUTH=eyJ0eXAiOiJKV1Q...（cURL 里的 x-fuyao-auth 头值，可不配，已内置 fallback）",
            "",
            "配置后重新运行：",
            "  python main.py --intraday-screen --intraday-theme-n 5 --intraday-max-candidates 5",
            "  python main.py --intraday-screen --intraday-codes 002185,600183",
            "=" * 100,
        ])
        logger.error("[IntradayScreen] cookie 预检抛错，拒绝执行")
        return 1, msg

    if not pre_trend or not pre_trend.get("points"):
        msg = "\n".join([
            "=" * 100,
            f"[IntradayScreen] cookie 预检失败：{pre_code} 抓不到分时数据",
            "可能原因：cookie 过期或接口失效（不一定报错，但返回空 quote_data）。",
            "",
            "请更新项目根目录 .env 文件中的：",
            "  INTRADAY_HX_COOKIE=user=MDpteF9...（cURL -b '...' 里的整段 cookie，注意保留分号和空格）",
            "  INTRADAY_HX_FUYAO_AUTH=eyJ0eXAiOiJKV1Q...（cURL 里的 x-fuyao-auth 头值，可不配，已内置 fallback）",
            "",
            "更新后重新运行：",
            "  python main.py --intraday-screen --intraday-theme-n 5 --intraday-max-candidates 5",
            "=" * 100,
        ])
        logger.error("[IntradayScreen] cookie 预检失败，拒绝执行（不进入选股阶段）")
        return 1, msg

    # 4b. 正式抓取（第一只复用预检结果，不重抓；其余每只前 sleep 2s）
    ok_count = 0
    total = len(candidate_codes)
    print(f"[IntradayScreen] cookie 有效，开始抓当日分时（同花顺 single_trend，限流 2s/只）...")

    if aggregate_trend_to_db(engine, pre_code, today, pre_trend):
        ok_count += 1
    print(f"[1/{total}] {pre_code} 完成（预检复用）", flush=True)

    for i, code in enumerate(candidate_codes[1:], start=2):
        time.sleep(2.0)  # 限流：每只前等 2 秒
        try:
            if aggregate_today_from_1min(engine, code, today):
                ok_count += 1
        except Exception as exc:
            logger.warning("[IntradayScreen] %s 分时聚合失败: %s", code, exc)
        print(f"[{i}/{total}] {code} 完成", flush=True)

    print(
        f"[IntradayScreen] 抓取完成: 分时聚合 {ok_count}/{total}"
    )

    # === Step 5: 跑规律选股 ===
    from .intraday_pattern_screener import IntradayPatternScreener
    from src.services.pattern_screener import (
        PatternScreener,
        PatternScreenerConfig,
    )

    intraday_screener = IntradayPatternScreener(engine=engine)
    candidates = intraday_screener.screen_intraday(
        config=PatternScreenerConfig(),
        stock_themes=stock_themes,
        stock_names=stock_names,
    )

    if not candidates:
        msg = "[IntradayScreen] 无符合条件的股票（评分后入选 0 只）"
        logger.warning(msg)
        return 0, msg

    # === Step 6: 洗盘精选（与 main.py --wash-v3 逻辑一致；无资金流） ===
    def _wash_final(c):
        return PatternScreener.compute_wash_final(c.score, c.wash_score)

    picked: List = []
    for c in candidates:
        f = _wash_final(c)
        if f is not None:
            c._wash_final = f
            picked.append(c)
    picked.sort(key=lambda c: -c._wash_final)
    logger.info("[IntradayScreen] 洗盘精选完成: %d 只", len(picked))

    # === Step 7: 格式化输出（slim=True 与 --wash-v3 一致） ===
    date_key = today.strftime("%Y%m%d")
    hot_themes_for_report = [(t[0], max(1, int(abs(t[1]) * 10))) for t in hot_themes]
    report = PatternScreener.format_report(
        picked, hot_themes_for_report, date_key=date_key, slim=True,
    )

    if hot_themes:
        concepts_line = f"实时热点概念: {', '.join(f'{t[0]}({t[1]:+.2f}%)' for t in hot_themes)}"
    else:
        concepts_line = f"候选股池: 指定 {len(candidate_codes)} 只 codes (--intraday-codes)"

    banner = "\n".join([
        "=" * 100,
        f"盘中选股报告（--wash-v3 等效，无资金流过滤）  {now.strftime('%Y-%m-%d %H:%M')}  (交易日 {today.strftime('%Y-%m-%d')})",
        concepts_line,
        f"候选股规模: {len(candidate_codes)} 只, 1min 聚合成功 {ok_count}",
        "=" * 100,
        "",
    ])

    final_report = banner + report
    return 0, final_report
