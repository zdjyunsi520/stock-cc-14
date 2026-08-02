# -*- coding: utf-8 -*-
"""
===================================
股票分析系统 - 主调度程序（精简版）
===================================

核心功能：
1. 增量同步日线 --sync-incremental
2. 洗盘选股+推送 --pattern-screen --wash --notify
3. 定时调度模式 --schedule

使用方式：
    python main.py --sync-incremental               # 增量同步日线
    python main.py --pattern-screen --wash --notify # 洗盘选股+推送
    python main.py --concept-fund-flow              # 抓取概念板块资金流向
    python main.py --sync-1min-kline                    # 同步 1 分钟分时（同花顺，~41 天/只，间隔 6s）
    python main.py --sync-1min-kline --sync-code 600519 # 仅同步指定股票（逗号分隔多个）
    python main.py --schedule                       # 启动定时调度
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from dotenv import dotenv_values
from src.config import setup_env

setup_env()

# 代理配置 - 通过 USE_PROXY 环境变量控制，默认关闭
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

from src.config import get_config, Config
from src.logging_config import setup_logging

logger = logging.getLogger(__name__)


def _setup_bootstrap_logging(debug: bool = False) -> None:
    """Initialize stderr-only logging before config is loaded."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if not any(
        isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stderr
        for h in root.handlers
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(handler)


def _setup_runtime_logging(log_dir: str, debug: bool = False) -> bool:
    """Switch to configured logging, falling back to console on file I/O errors."""
    try:
        setup_logging(log_prefix="stock_analysis", debug=debug, log_dir=log_dir)
        return True
    except OSError as exc:
        logger.warning(
            "文件日志初始化失败，已降级为控制台日志输出；日志目录 %r 当前不可写或不可创建: %s。"
            "官方 Docker 镜像启动入口会自动修复默认挂载目录权限；若仍失败，"
            "请检查是否使用了 --user、只读挂载、rootless Docker 或 NFS 等限制写入的环境。",
            log_dir,
            exc,
        )
        return False


def _parse_codes(raw: str | None) -> List[str] | None:
    """解析 --code 入参为 6 位股票代码列表。

    支持逗号或空格分隔多只（如 '600519,000001'），自动去除空白与重复，
    过滤掉非 6 位数字的非法项并打 warning。空入参返回 None（表示不限定）。
    """
    if not raw:
        return None
    parts: List[str] = []
    for chunk in raw.replace(' ', ',').split(','):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    valid = [p for p in parts if p.isdigit() and len(p) == 6]
    invalid = [p for p in parts if p not in valid]
    if invalid:
        logger.warning("忽略非法 code（非 6 位数字）: %s", invalid)
    # 去重并保持首次出现顺序（run_1min_kline_sync 内部还会做一层防御性去重）
    return list(dict.fromkeys(valid)) or None


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数 parser（供 parse_arguments 与 print_help 复用，避免重复定义）。"""
    parser = argparse.ArgumentParser(
        description='股票分析系统 - 精简版',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例: python main.py --sync-incremental | --pattern-screen --wash --notify | --schedule"
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式，输出详细日志'
    )

    parser.add_argument(
        '--date',
        type=str,
        default=None,
        help='指定数据日期，如 20260605（默认取数据库最新交易日）'
    )

    parser.add_argument(
        '--force-refresh',
        action='store_true',
        help='强制重新分析（忽略缓存）'
    )

    parser.add_argument(
        '--sync-incremental',
        action='store_true',
        help='增量同步已下载股票的最新日线数据'
    )

    parser.add_argument(
        '--fund-flow',
        action='store_true',
        help='同步资金流向数据：无数据则全量抓取30日，有数据则增量更新最新交易日（加--force强制全量）'
    )

    parser.add_argument(
        '--concept-fund-flow',
        action='store_true',
        help='抓取概念板块资金流向（同花顺 gnzjl 接口，约385个概念，存入 stock_concept_fund_flow 表）'
    )

    parser.add_argument(
        '--sync-concept-membership',
        action='store_true',
        help='同步概念板块成分股到 stock_concept_membership 表（每题材最多200只，377题材约40分钟）'
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='限制数量（如 --sync-concept-membership --limit 5 仅拉5个题材测试）'
    )

    parser.add_argument(
        '--sync-1min-kline',
        action='store_true',
        help='同步 1 分钟分时（同花顺 d.10jqka all.js，~41 天 9881 条/只，间隔 6s）'
    )

    parser.add_argument(
        '--sync-code',
        type=str,
        default=None,
        metavar='CODE[,CODE...]',
        help='配合 --sync-1min-kline 使用：仅同步指定股票（逗号或空格分隔多个，如 --sync-code 600519,000001）；'
             '不传则同步全部已同步股票'
    )

    parser.add_argument(
        '--detect-accumulation',
        type=str,
        default=None,
        metavar='CODE',
        help='分时吸筹侦测（基于 1min K 线的 FLAT 段七阶段评分，需配合 --start/--end）'
    )

    parser.add_argument(
        '--smart-money',
        type=str,
        default=None,
        nargs='?',
        const='__all__',
        metavar='CODE',
        help='主力压价吸筹扫描：不带 CODE 全量扫描隐蔽吸筹（大单流入+股价不涨）；'
             '带 CODE 单股五阶段详评（吸筹/洗盘/二次吸筹/主升浪/派发）'
    )

    parser.add_argument(
        '--detect-anomaly',
        type=str,
        default=None,
        metavar='CODE',
        help='压价吸筹异常时段侦测（单股）：big_net>0 AND small_net<0 AND pct<3%% 触发，'
             '隔一两天算同一时段（聚类输出全部异常时段）'
    )

    parser.add_argument(
        '--max-gap',
        type=int,
        default=2,
        help='--detect-anomaly 时段内允许的最大连续未触发天数（默认2，即"隔一两天"）'
    )

    parser.add_argument(
        '--detect-anomaly-all',
        action='store_true',
        help='全量压价吸筹异常时段聚类：扫所有股票→按时段聚类→反推热门概念'
    )

    parser.add_argument(
        '--cluster-gap',
        type=int,
        default=2,
        help='--detect-anomaly-all 跨股票时段 start_date 局部相邻阈值（默认2天）'
    )

    parser.add_argument(
        '--max-span-days',
        type=int,
        default=7,
        help='--detect-anomaly-all 单个聚类最大自然日跨度（默认7，强制拆分）'
    )

    parser.add_argument(
        '--min-codes',
        type=int,
        default=10,
        help='--detect-anomaly-all 聚类最少股票数（默认10，过滤单股噪音）'
    )

    parser.add_argument(
        '--days',
        type=int,
        default=5,
        help='--smart-money 全量扫描的统计窗口天数（默认5）'
    )

    parser.add_argument(
        '--top',
        type=int,
        default=30,
        help='--smart-money 全量扫描显示的 Top N（默认30）'
    )

    parser.add_argument(
        '--find-doji',
        action='store_true',
        help='十字星查询（只返回满分=100）：默认最近一个交易日全市场；'
             '--date YYYYMMDD 返回指定日期的满分十字星；'
             '--code CODE 返回指定股票的所有满分十字星日期；'
             '--all 返回所有历史满分十字星（代码/名称/日期）'
    )

    parser.add_argument(
        '--all',
        action='store_true',
        help='配合 --find-doji：扫描所有股票的全部 1min 历史'
    )

    parser.add_argument(
        '--code',
        type=str,
        default=None,
        metavar='CODE',
        help='配合 --find-doji：指定股票代码，返回该股票所有十字星日期'
    )

    parser.add_argument(
        '--start',
        type=str,
        default=None,
        help='开始日期 YYYY-MM-DD 或 YYYYMMDD（配合 --detect-accumulation 使用）'
    )

    parser.add_argument(
        '--end',
        type=str,
        default=None,
        help='结束日期 YYYY-MM-DD 或 YYYYMMDD（配合 --detect-accumulation 使用）'
    )

    parser.add_argument(
        '--max-stocks',
        type=int,
        default=None,
        help='限制处理的股票数量（配合 --fund-flow 或 --sync-incremental 使用）'
    )

    parser.add_argument(
        '--with-webui',
        action='store_true',
        help='同时启动 WebUI 子进程（默认端口 8000，可用 WEBUI_HOST/WEBUI_PORT 环境变量覆盖）'
    )

    parser.add_argument(
        '--pattern-screen',
        action='store_true',
        help='规律选股（基于涨跌基因分析的持续热点规律筛选股票）'
    )

    parser.add_argument(
        '--wash',
        action='store_true',
        help='洗盘精选模式（v3算法：>60加洗盘,50-60高洗减,<=50强洗加），自动接资金流分析'
    )

    parser.add_argument(
        '--wash-v3',
        action='store_true',
        help='洗盘选股V3：纯洗盘精选（输出格式同 --wash），不接资金流分析'
    )

    parser.add_argument(
        '--wash-v4',
        type=str,
        default=None,
        metavar='DATE',
        help='洗盘选股回测V4：在 --wash-backtest 基础上加主升浪形态过滤'
             '（洗盘精选后用 surge 形态硬过滤；不通过的进入 rejected_surge 档；可用 --surge-* 参数调阈值）'
    )

    parser.add_argument(
        '--wash-backtest',
        type=str,
        default=None,
        metavar='DATE',
        help='洗盘选股回测：复现 T=DATE 日的 --wash 买入列表，统计 T+1/T+3/T+5 累计涨幅（DATE: YYYYMMDD）'
    )

    parser.add_argument(
        '--intraday-screen',
        action='store_true',
        help='盘中选股（9:30 开盘后运行；交易日=今天；算法同 --pattern-screen --wash-v3 纯洗盘无资金流，1min 走 tushare stk_mins，数据走独立 tmp 库）'
    )

    parser.add_argument(
        '--intraday-theme-n',
        type=int,
        default=20,
        help='盘中选股取实时涨幅前 N 个概念（默认 20，与 --pattern-screen 的 concept_top_n 一致）'
    )

    parser.add_argument(
        '--intraday-max-candidates',
        type=int,
        default=0,
        help='盘中选股最多处理的成分股数量（默认 0=不限，处理全部获取到的成分股；冒烟测试可传 5 等小值加速）'
    )

    parser.add_argument(
        '--intraday-codes',
        type=str,
        default=None,
        help='盘中选股指定股票代码（逗号分隔，如 "002185,600183"）；指定后跳过实时热点概念获取，直接对这些股票实时评分'
    )

    parser.add_argument(
        '--sideways-screen',
        action='store_true',
        help='横盘选股：找最近 N 天内曾横盘过的股票（滑动子窗口扫描，双维度判定 斜率≈0 + 最长连续同向<N）'
    )

    parser.add_argument(
        '--sideways-days',
        type=int,
        default=30,
        help='横盘选股扫描总窗口（默认 30 天：在这 30 天内找横盘段）'
    )

    parser.add_argument(
        '--sideways-min-len',
        type=int,
        default=5,
        help='横盘段实际天数必须 > 此值（默认 >5 天）'
    )

    parser.add_argument(
        '--sideways-min-amount',
        type=float,
        default=1.0,
        help='横盘段日均成交额 ≥ 此值（亿元，默认 1.0）'
    )

    parser.add_argument(
        '--sideways-slope',
        type=float,
        default=0.1,
        help='横盘选股每日斜率/close均值 阈值（默认 0.1%%）'
    )

    parser.add_argument(
        '--sideways-streak',
        type=int,
        default=5,
        help='横盘选股最长连续同向天数阈值（默认 < 5 天）'
    )

    parser.add_argument(
        '--accumulation-screen',
        action='store_true',
        help='资金吸筹选股：最近 N 天每天涨≤3%%（下跌不限）且累计资金净流入>0，按累计净流入降序'
    )

    parser.add_argument(
        '--accumulation-days',
        type=int,
        default=5,
        help='吸筹选股回看天数（默认 5）'
    )

    parser.add_argument(
        '--accumulation-rise-max',
        type=float,
        default=3.0,
        help='吸筹选股每日涨幅上限（默认 3.0%%，下跌不限）'
    )

    parser.add_argument(
        '--accumulation-min-amount',
        type=float,
        default=1.0,
        help='吸筹选股 N 天日均成交额下限（亿元，默认 1.0）'
    )

    # === 主升浪选股 ===
    parser.add_argument(
        '--主升浪选股',
        dest='surge_screen',
        action='store_true',
        help='主升浪选股：剔除1最高+1最低噪声后，识别"低量吸筹洗盘 → 高量主升浪"形态，'
             '按 (高量组均量÷低量组均量) 倍率降序输出'
    )

    parser.add_argument(
        '--surge-days',
        dest='surge_days',
        type=int,
        default=20,
        help='主升浪选股分析窗口（默认 20 天：在这 20 天内做极端值剔除后找 L/H）'
    )

    parser.add_argument(
        '--surge-ratio-min',
        dest='surge_ratio_min',
        type=float,
        default=1.9,
        help='H/L 门槛（默认 1.9：高量组均量÷低量组均量 ≥ 1.9 才入选）'
    )

    parser.add_argument(
        '--surge-low-ratio',
        dest='surge_low_ratio',
        type=float,
        default=1.9,
        help='低量组扩展阈值系数（默认 1.9：volume ≤ L×1.9 视为低量，归吸筹洗盘组）'
    )

    parser.add_argument(
        '--surge-extend-ratio',
        dest='surge_extend_ratio',
        type=float,
        default=0.5,
        help='高量组向后扩展阈值（默认 0.5：H 之后 vol ≥ H×0.5 继续纳入高量组）'
    )

    parser.add_argument(
        '--surge-max-break-days',
        dest='surge_max_break_days',
        type=int,
        default=2,
        help='高量组扩展断点容忍天数（默认 2：≤2 天 vol<H/2 可被后续高量日桥接，断点也计入高量组）'
    )

    parser.add_argument(
        '--surge-min-low-len',
        dest='surge_min_low_len',
        type=int,
        default=3,
        help='低量组最少天数（默认 3 天，避免单日孤点）'
    )

    parser.add_argument(
        '--surge-min-amount',
        dest='surge_min_amount',
        type=float,
        default=1.0,
        help='全窗口日均成交额下限（亿元，默认 1.0，过滤僵尸股）'
    )

    parser.add_argument(
        '--surge-min-h-amount',
        dest='surge_min_h_amount',
        type=float,
        default=3.0,
        help='H 日成交额下限（亿元，默认 3.0，保证主升浪有量）'
    )

    parser.add_argument(
        '--surge-max-candidates',
        dest='surge_max_candidates',
        type=int,
        default=50,
        help='主升浪选股最多输出候选数（默认 50）'
    )

    parser.add_argument(
        '--surge-codes',
        dest='surge_codes',
        type=str,
        default=None,
        help='主升浪选股指定股票池（逗号分隔，如 "002371,688012,300604"）；'
             '指定后只扫这些票且跳过板块黑名单（适用于科创板/创业板的板块分析）'
    )

    parser.add_argument(
        '--surge-theme-narrow',
        dest='surge_theme_narrow',
        action='store_true',
        default=False,
        help='主升浪选股缩池到 T 日持续热点成分股（与洗盘选股同算法）：'
             '走 PatternScreener._find_persistent_themes 找出最近5交易日里至少3天进涨幅top5的题材，'
             '再反查这些题材的成分股作为白名单。与 --surge-codes 取交集'
    )

    parser.add_argument(
        '--notify',
        action='store_true',
        help='规律选股结果推送到飞书'
    )

    parser.add_argument(
        '--schedule',
        action='store_true',
        help='启用定时任务模式（16:30洗盘选股推送, 08:30早盘推送; 启动时立即补跑一次; 数据按需实时获取）'
    )

    return parser


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    return _build_parser().parse_args()


def run_cache_concepts(args: argparse.Namespace) -> int:
    """已废弃：--cache-concepts 持久化路径已下线，概念池统一走 stock_concept_membership DB。

    保留函数避免老脚本/外部调用 AttributeError，但内部直接报错退出并给出迁移指引。
    """
    logger.error("[Deprecated] --cache-concepts 已废弃：持续热点现在直接读 stock_concept_membership DB")
    print(
        "[Deprecated] --cache-concepts 已废弃\n"
        "持续热点现在统一通过 PersistentThemeFinder 读 stock_concept_membership DB，\n"
        "不再生成 concept_universe_*.json。\n"
        "如需补充概念成分股数据，请运行：python main.py --sync-concept-membership"
    )
    return 1


def _kill_duplicate_process(keyword: str) -> None:
    """杀掉命令行包含 keyword 的旧 python 进程（排除自身）。"""
    import subprocess, os
    own_pid = os.getpid()
    try:
        out = subprocess.check_output(
            'wmic process where "name=\'python.exe\'" get ProcessId,CommandLine /format:csv',
            shell=True, text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.strip().split('\n'):
            parts = line.strip().split(',', 1)
            if len(parts) < 2:
                continue
            cmd = parts[0]
            try:
                pid = int(parts[1].strip())
            except ValueError:
                continue
            if pid != own_pid and keyword in cmd:
                subprocess.call(f'taskkill /F /PID {pid}', shell=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("已杀掉旧进程 PID=%d (匹配: %s)", pid, keyword)
    except Exception as exc:
        logger.debug("检查旧进程失败: %s", exc)


def run_sync_incremental() -> int:
    """增量同步日线数据（剔除板块黑名单 + ST/*ST/退市）。"""
    logger.info("模式: 增量日线数据同步")
    from src.services.daily_data_sync_service import DailyDataSyncService
    from src.storage import DatabaseManager

    db = DatabaseManager()

    # Step 1: 取全部已完成股票，过滤掉板块黑名单 + ST/退市
    all_states = db.get_sync_states(status="done")
    name_map = {s["code"]: s.get("code_name") or "" for s in all_states}

    excluded_detail: List[str] = []
    kept_codes: List[str] = []
    for code, name in name_map.items():
        reason = _get_exclude_reason(code, name)
        if reason:
            excluded_detail.append(f"{code}({name or '-'}:{reason})")
            continue
        kept_codes.append(code)

    if excluded_detail:
        logger.info(
            "[增量同步过滤] 剔除 %d 只黑名单股票: %s",
            len(excluded_detail), ", ".join(excluded_detail[:20]),
        )
        print(
            f"[增量同步过滤] 候选池剔除 {len(excluded_detail)} 只黑名单股票"
            f"（300/301 创业板 + 688 科创板 + 4/8 北交所 + ST/*ST/退市），"
            f"剩余 {len(kept_codes)} 只",
            flush=True,
        )

    if not kept_codes:
        print("候选池过滤后为空，跳过增量同步", flush=True)
        return 0

    # Step 2: 把过滤后的 codes 传给 service
    config = get_config()
    svc = DailyDataSyncService(config=config)
    result = svc.sync_incremental(codes=kept_codes)
    logger.info(
        "增量同步完成: total=%d synced=%d failed=%d rows=%d (剔除黑名单 %d 只)",
        result.total, result.synced, result.failed, result.rows_written,
        len(excluded_detail),
    )
    print(
        f"增量同步完成: total={result.total} synced={result.synced} "
        f"failed={result.failed} rows={result.rows_written} "
        f"(已剔除黑名单 {len(excluded_detail)} 只)"
    )
    return 0


def run_sync_concept_membership(limit: int | None = None) -> int:
    """同步概念板块成分股到 stock_concept_membership 表。"""
    logger.info("模式: 同步概念板块成分股")
    from scripts.scrape_concept_membership import main as scrape_main
    import sys as _sys

    argv = ["scrape_concept_membership"]
    if limit:
        argv.extend(["--limit", str(limit)])
    # 复用脚本的 main()
    old_argv = _sys.argv
    _sys.argv = argv
    try:
        return scrape_main()
    finally:
        _sys.argv = old_argv


def run_concept_fund_flow_sync(force: bool = False) -> int:
    """抓取同花顺概念板块资金流向（自动判定交易日，约 385 个概念）。

    交易日判定：
    - 15:30 后 + 工作日 + today >= stock_daily.MAX(date) → today
    - 其他情况 → stock_daily.MAX(date)（自动跳过周末和已知节假日）

    重复抓取：当日已抓则跳过（用 --force-refresh 强制重抓）。
    """
    logger.info("模式: 概念板块资金流向抓取")
    from scripts.scrape_concept_fund_flow import fetch_concept_fund_flow, save_concept_fund_flow
    from src.storage import DatabaseManager, StockConceptFundFlow
    from src.utils.trading_date import TradingDate

    db = DatabaseManager()
    session = db.get_session()
    try:
        trade_date = TradingDate.get_target_trade_date(session)
    finally:
        session.close()
    logger.info("目标交易日: %s", trade_date)

    # 当天已抓则跳过（除非 force）
    if not force:
        session = db.get_session()
        try:
            existing = session.query(StockConceptFundFlow).filter_by(date=trade_date).first()
        finally:
            session.close()
        if existing:
            msg = f"当日已抓取，跳过 (date={trade_date})。如需重抓请加 --force-refresh"
            logger.info(msg)
            print(msg, flush=True)
            return 0

    import requests as _req
    http = _req.Session()
    data = fetch_concept_fund_flow(http)
    if not data:
        logger.warning("概念板块资金流向抓取失败：无数据")
        print("抓取失败：无数据", flush=True)
        return 1

    n = save_concept_fund_flow(db, data, trade_date)

    top3 = sorted(data, key=lambda r: r.get("net_amount") or 0, reverse=True)[:3]
    top3_str = ", ".join(
        f"{r['concept']}({r['net_amount']:.1f}亿)" for r in top3 if r.get("net_amount")
    )
    summary = f"概念资金流向抓取完成: date={trade_date} saved={n} TOP3=[{top3_str}]"
    logger.info(summary)
    print(summary, flush=True)
    return 0


def _fmt_dur(seconds: float) -> str:
    """格式化耗时：24s / 3m25s / 1h23m"""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60:02d}m"


@contextlib.contextmanager
def _capture_stdout_to_logger():
    """把 with 块内的 print(...) 转发到 logger.info。

    用于后台子线程的 1min K 线同步：原 print 只到 stdout，登录终端可见但不进
    日志文件；包裹后 print 同时进 console（logger 的 StreamHandler）和文件
    （RotatingFileHandler），且 logger emit 时强制 flush，绕过 stdout 缓冲。
    """
    import io

    class _Tee(io.StringIO):
        def write(self, s: str) -> int:
            if s and s.strip():
                logger.info(s.rstrip())
            return len(s)

        def flush(self) -> None:
            pass

    with contextlib.redirect_stdout(_Tee()):
        yield


def _is_excluded_board(code: str) -> bool:
    """板块黑名单：300/301 创业板、688 科创板、4*/8* 北交所。

    代理到 src.utils.stock_filter.is_excluded_board（保留旧名向后兼容）。
    """
    from src.utils.stock_filter import is_excluded_board
    return is_excluded_board(code)


def _is_excluded_by_name(name: str) -> bool:
    """名称黑名单：ST/*ST/退市。

    代理到 src.utils.stock_filter.is_excluded_by_name（保留旧名向后兼容）。
    """
    from src.utils.stock_filter import is_excluded_by_name
    return is_excluded_by_name(name)


def _get_exclude_reason(code: str, name: str) -> str:
    """返回排除原因字符串；空串表示不排除。

    代理到 src.utils.stock_filter.get_exclude_reason（保留旧名向后兼容）。
    """
    from src.utils.stock_filter import get_exclude_reason
    return get_exclude_reason(code, name)


def _cleanup_excluded_1min_codes(
    session,
    name_map: Dict[str, str],
) -> tuple:
    """从 stock_1min_kline 物理删除板块黑名单 + ST/退市股票的全部历史数据。

    幂等：多次执行结果一致，已删完时再跑返回 0/0。

    Args:
        session: 已开启的 SQLAlchemy session（由调用方 commit/rollback）
        name_map: {code: name}，用于找出 ST/退市的 code 列表

    Returns:
        (board_deleted_rows, st_deleted_rows)
    """
    from sqlalchemy import text

    # 板块黑名单：DELETE LIKE（前缀匹配，code 字段有索引）
    board_deleted = session.execute(text(
        "DELETE FROM stock_1min_kline "
        "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%' "
        "OR code LIKE '4%' OR code LIKE '8%'"
    )).rowcount

    # ST/退市：先从 name_map 找出 code 列表，分批 DELETE IN（避免 SQL 参数上限）
    st_codes = [c for c, name in name_map.items() if _is_excluded_by_name(name)]
    st_deleted = 0
    if st_codes:
        for i in range(0, len(st_codes), 500):
            batch = st_codes[i: i + 500]
            placeholders = ",".join(f":c{j}" for j in range(len(batch)))
            params = {f"c{j}": c for j, c in enumerate(batch)}
            st_deleted += session.execute(text(
                f"DELETE FROM stock_1min_kline WHERE code IN ({placeholders})"
            ), params).rowcount

    return int(board_deleted or 0), int(st_deleted or 0)


def run_1min_kline_sync(
    max_stocks: int | None = None,
    force: bool = False,
    codes: List[str] | None = None,
) -> int:
    """1 分钟分时同步（同花顺 d.10jqka，~41 天 9881 条/只，间隔 10 秒）。

    对每只股票：
    - **跳过**：本地最新 ts 的日期 >= stock_daily.MAX(date) → 已是最新交易日
    - **全量**：本地无数据 → fetch_1min_kline（单次拿全部 ~9881 条）
    - **增量**：本地有数据但落后 → fetch_1min_kline（save 用 upsert 自动补齐）

    板块黑名单（默认开启）：
    - 300/301 创业板、688 科创板、4*/8* 北交所
    - ST/*ST/退市股
    - 同步前会先物理清理 stock_1min_kline 中这些股票的历史数据，再过滤候选池

    Args:
        max_stocks: 限制处理的股票数量
        force: 强制全部重新抓取（忽略跳过判断）
        codes: 仅同步指定股票（6 位代码列表）；为 None 时同步全部已同步股票，
            不要求股票已在 sync_state 中（name 缺失时显示空串）
    """
    logger.info("模式: 1 分钟分时同步（同花顺）")
    from scripts.scrape_1min_kline import fetch_1min_kline, save_1min_kline
    from src.storage import DatabaseManager, Stock1minKline
    from src.utils.trading_date import TradingDate
    from sqlalchemy import text

    db = DatabaseManager()
    session = db.get_session()
    try:
        # 目标交易日：与 cache_concepts/fund_flow 保持一致，避免 stock_daily 还没同步时拿不到今天
        latest_trade_date = TradingDate.get_target_trade_date(session)
        logger.info("最新交易日: %s", latest_trade_date)

        # name_map 全量预取，供两种模式复用（指定 code 模式下找不到 name 时回退空串）
        all_states = db.get_sync_states(status="done")
        name_map = {s["code"]: s.get("code_name") or "" for s in all_states}

        # === Step 0: 物理清理 stock_1min_kline 中的黑名单股票历史数据 ===
        # 板块黑名单 + ST/退市：先 DELETE 已存在的脏数据，再过滤候选池
        board_deleted, st_deleted = _cleanup_excluded_1min_codes(session, name_map)
        if board_deleted or st_deleted:
            session.commit()  # DELETE 必须显式 commit，否则 close 时回滚
            logger.info(
                "[1min清理] 删除黑名单股票历史: 板块=%d行 ST=%d行",
                board_deleted, st_deleted,
            )
            print(
                f"[1min清理] 删除黑名单股票历史数据: 板块黑名单 {board_deleted} 行，ST/退市 {st_deleted} 行",
                flush=True,
            )

        if codes:
            # 指定 code 模式：6 位补零 + 去重，不依赖 sync_state 列表
            all_codes = list(dict.fromkeys(str(c).zfill(6) for c in codes))
            # 立即向 stdout 反馈（避免在 5400 万行表上做 GROUP BY 时长时间无输出）
            print(
                f"指定 code 模式: 仅同步 {len(all_codes)} 只 {','.join(all_codes)}，"
                f"正在查询本地最新 ts...",
                flush=True,
            )
        else:
            if not all_states:
                logger.warning("无已同步股票，跳过 1min K 线抓取")
                return 0
            all_codes = [s["code"] for s in all_states]

        # === Step 0.5: 候选池过滤：剔除板块黑名单 + ST ===
        excluded_detail: List[str] = []
        filtered_codes: List[str] = []
        for code in all_codes:
            reason = _get_exclude_reason(code, name_map.get(code, ""))
            if reason:
                excluded_detail.append(f"{code}({reason})")
                continue
            filtered_codes.append(code)
        if excluded_detail:
            logger.info(
                "[1min过滤] 候选池剔除 %d 只黑名单股票: %s",
                len(excluded_detail), ", ".join(excluded_detail[:20]),
            )
            print(
                f"[1min过滤] 候选池剔除 {len(excluded_detail)} 只黑名单股票"
                f"（板块黑名单=300/301/688/4/8 + ST/退市），剩余 {len(filtered_codes)} 只",
                flush=True,
            )
        all_codes = filtered_codes
        if not all_codes:
            print("候选池过滤后为空，跳过抓取", flush=True)
            return 0

        if codes:
            # 指定 code 模式：用 WHERE IN 限定查询范围，避免 5400 万行全表 GROUP BY（~9 分钟）
            # 实测：全表 GROUP BY 568s vs WHERE IN 单只 6ms（9 万倍差距）
            placeholders = ",".join(f":c{i}" for i in range(len(all_codes)))
            params = {f"c{i}": c for i, c in enumerate(all_codes)}
            rows = session.execute(
                text(
                    f"SELECT code, MAX(ts) FROM stock_1min_kline "
                    f"WHERE code IN ({placeholders}) GROUP BY code"
                ),
                params,
            ).fetchall()
        else:
            # 全量模式：仍需全表 GROUP BY 一次（结果缓存到字典，循环内 O(1) 查询）
            rows = session.execute(
                text("SELECT code, MAX(ts) FROM stock_1min_kline GROUP BY code")
            ).fetchall()
        existing_max_ts = {r[0]: r[1] for r in rows}
    finally:
        session.close()

    latest_str = latest_trade_date.isoformat() if hasattr(latest_trade_date, 'isoformat') else str(latest_trade_date)[:10]
    skip_codes: List[str] = []
    todo_codes: List[str] = []
    for code in all_codes:
        if force:
            todo_codes.append(code)
            continue
        last_ts = existing_max_ts.get(code)
        last_date_str = str(last_ts)[:10] if last_ts else None
        if last_date_str and last_date_str >= latest_str:
            skip_codes.append(code)
        else:
            todo_codes.append(code)

    if max_stocks:
        todo_codes = todo_codes[:max_stocks]

    full_cnt = sum(1 for c in todo_codes if c not in existing_max_ts)
    incr_cnt = len(todo_codes) - full_cnt

    logger.info(
        "待抓 %d 只（全量=%d 增量=%d），跳过 %d 只（已是最新交易日 %s），force=%s",
        len(todo_codes), full_cnt, incr_cnt, len(skip_codes), latest_trade_date, force,
    )

    if not todo_codes:
        print(f"全部已是最新交易日，无需更新（跳过 {len(skip_codes)} 只）", flush=True)
        return 0

    total, saved, failed, rows_written = len(todo_codes), 0, 0, 0
    start_time = time.time()

    for idx, code in enumerate(todo_codes):
        is_full = code not in existing_max_ts
        mode_label = "全量" if is_full else "增量"
        name = name_map.get(code, "")
        pct = (idx + 1) / total * 100
        try:
            data = fetch_1min_kline(code)
            if data:
                fetched = len(data)
                n = save_1min_kline(db, data)
                saved += 1
                rows_written += n
                first_ts = data[0]["ts"]
                last_ts = data[-1]["ts"]
                span_days = (last_ts - first_ts).days + 1
                first_short = first_ts.strftime("%m-%d")
                last_short = last_ts.strftime("%m-%d")
                elapsed = time.time() - start_time
                remaining = elapsed / (idx + 1) * (total - idx - 1)
                print(
                    f"[{idx+1}/{total}] {pct:5.1f}% | {code} {name} | {mode_label} "
                    f"抓{fetched}/新增{n} {first_short}→{last_short}({span_days}天) | "
                    f"累计新增{rows_written}条 用时{_fmt_dur(elapsed)} 剩余≈{_fmt_dur(remaining)}",
                    flush=True,
                )
            else:
                failed += 1
                elapsed = time.time() - start_time
                print(
                    f"[{idx+1}/{total}] {pct:5.1f}% | {code} {name} | {mode_label} "
                    f"无数据 | 用时{_fmt_dur(elapsed)}",
                    flush=True,
                )
        except Exception as exc:
            failed += 1
            elapsed = time.time() - start_time
            print(
                f"[{idx+1}/{total}] {pct:5.1f}% | {code} {name} | {mode_label} "
                f"失败: {exc} | 用时{_fmt_dur(elapsed)}",
                flush=True,
            )
        if idx < total - 1:
            time.sleep(6.0)  # 同花顺间隔 6 秒，避免触发限流

    elapsed = time.time() - start_time
    avg = elapsed / total if total else 0
    summary = (
        f"1min K 线同步完成: total={total} saved={saved} failed={failed} "
        f"新增={rows_written} skipped={len(skip_codes)} "
        f"用时={_fmt_dur(elapsed)} 均{avg:.1f}s/只"
    )
    logger.info(summary)
    print(summary, flush=True)
    return 0


def run_fund_flow_sync(max_stocks: int | None = None, force: bool = False) -> int:
    """资金流向 2合1 同步：按每只股票的状态分别判定。

    对每只股票：
    - 已有最新交易日的资金数据 → 跳过（不请求）
    - 部分历史数据但缺最近几天 → 重新抓取（同花顺固定 30 天，save_fund_flow upsert 自动补齐缺失天数）
    - 完全无数据 → 抓取 30 天历史

    Args:
        max_stocks: 限制处理的股票数量
        force: 强制全部重新抓取（忽略已有数据）
    """
    from scripts.scrape_fund_flow import fetch_fund_flow, save_fund_flow
    from src.storage import DatabaseManager
    from src.utils.trading_date import TradingDate
    from sqlalchemy import text

    db = DatabaseManager()
    session = db.get_session()
    try:
        # 1. 目标交易日：与 cache_concepts 保持一致，避免 stock_daily 未同步时拿不到今天
        latest_trade_date = TradingDate.get_target_trade_date(session)
        logger.info("最新交易日: %s", latest_trade_date)

        # 2. 取所有已同步的股票（带 name）—— 过滤黑名单（300/301/688/4/8 + ST/退市）
        all_states = db.get_sync_states(status="done")
        if not all_states:
            logger.warning("无已同步股票，跳过资金流向抓取")
            return 0
        name_map = {s["code"]: s.get("code_name") or "" for s in all_states}
        excluded_detail: List[str] = []
        kept_codes_pre: List[str] = []
        for code in (s["code"] for s in all_states):
            reason = _get_exclude_reason(code, name_map.get(code, ""))
            if reason:
                excluded_detail.append(f"{code}({reason})")
                continue
            kept_codes_pre.append(code)
        if excluded_detail:
            logger.info(
                "[资金流过滤] 候选池剔除 %d 只黑名单股票", len(excluded_detail),
            )
            print(
                f"[资金流过滤] 候选池剔除 {len(excluded_detail)} 只黑名单股票，剩余 {len(kept_codes_pre)} 只",
                flush=True,
            )
        all_codes = kept_codes_pre

        # 3. 取每只股票现有资金数据的最新日期
        rows = session.execute(
            text("SELECT code, MAX(date) FROM stock_fund_flow GROUP BY code")
        ).fetchall()
        existing_max = {r[0]: r[1] for r in rows}
    finally:
        session.close()

    # 4. 过滤：跳过最新交易日已有数据的股票（force=True 时跳过过滤）
    if force:
        codes = list(all_codes)
        skipped = 0
    else:
        latest_iso = latest_trade_date.isoformat() if hasattr(latest_trade_date, 'isoformat') else str(latest_trade_date)[:10]
        codes = [c for c in all_codes if str(existing_max.get(c))[:10] != latest_iso]
        skipped = len(all_codes) - len(codes)

    if max_stocks:
        codes = codes[:max_stocks]

    logger.info(
        "待抓取 %d 只，跳过 %d 只（最新交易日 %s 已有数据），force=%s",
        len(codes), skipped, latest_trade_date, force,
    )

    if not codes:
        print(f"全部已抓取，无需更新（跳过 {skipped} 只）", flush=True)
        return 0

    import requests as _req
    http = _req.Session()
    total, saved, failed, rows_written = len(codes), 0, 0, 0
    start_time = time.time()

    for idx, code in enumerate(codes):
        name = name_map.get(code, "")
        try:
            data = fetch_fund_flow(code, http)
            if data:
                n = save_fund_flow(db, data)
                saved += 1
                rows_written += n
                latest = data[0].get("date", "")
                print(f"[{idx+1}/{total}] {code} {name:<8} | {n} 条已保存 | 最新 {latest}", flush=True)
            else:
                failed += 1
                print(f"[{idx+1}/{total}] {code} {name:<8} | 无数据", flush=True)
        except Exception as exc:
            failed += 1
            print(f"[{idx+1}/{total}] {code} {name:<8} | 失败 - {exc}", flush=True)
        if idx < total - 1:
            time.sleep(3.0)

    elapsed = time.time() - start_time
    summary = (
        f"资金流向抓取完成: total={total} saved={saved} failed={failed} "
        f"rows={rows_written} skipped={skipped} 耗时={elapsed:.0f}s"
    )
    logger.info(summary)
    print(summary, flush=True)
    return 0


def run_accumulation_detect(args: argparse.Namespace) -> int:
    """分时吸筹侦测（基于 1min K 线的 FLAT 段七阶段评分）。"""
    logger.info("模式: 分时吸筹侦测")
    from datetime import timedelta
    from src.services.minute_accumulation_detector import (
        MinuteAccumulationDetector, format_results,
    )
    from src.storage import DatabaseManager, StockFundFlow

    code = args.detect_accumulation
    if not code:
        logger.error("必须指定 --detect-accumulation CODE")
        return 1
    if not args.start or not args.end:
        logger.error("必须同时指定 --start 和 --end")
        return 1

    code = code.zfill(6)
    detector = MinuteAccumulationDetector()
    results = detector.detect(code, start=args.start, end=args.end)

    print(f"\n{code} 在 {args.start} ~ {args.end} 的 FLAT 段吸筹分析 ({len(results)} 个):\n")
    print(format_results(results))

    if not results:
        return 0

    # 第二张表：每个交易日的资金分析 + 次日涨跌
    res_by_date = {r.date: r for r in results}
    event_days = sorted(res_by_date.keys())

    db = DatabaseManager()
    session = db.get_session()
    try:
        first_day = min(event_days)
        last_day = max(event_days) + timedelta(days=7)
        flows_all = session.query(StockFundFlow).filter(
            StockFundFlow.code == code,
            StockFundFlow.date >= first_day,
            StockFundFlow.date <= last_day,
        ).order_by(StockFundFlow.date).all()
    finally:
        session.close()

    flow_by_date = {f.date: f for f in flows_all}
    sorted_dates = sorted(flow_by_date.keys())

    def _next_trading_day(d):
        for nd in sorted_dates:
            if nd > d:
                return flow_by_date[nd]
        return None

    print(f"\n{code} 价格档吸筹 + 资金 + 次日验证（共 {len(event_days)} 个交易日）:\n")
    for d in event_days:
        r = res_by_date[d]
        bd = f"{r.score_pos}/{r.score_decay}/{r.score_consec}"
        seg_summary = (
            f"评分 {r.score} ({r.state}) 明细 {bd}(体/影/双)  "
            f"开{r.open_price:.2f} 收{r.open_price + r.close_vs_avg/100*r.price_ref:.2f} "
            f"实体{r.body_pct:.2f}% 影线{r.wick_ratio:.0f}% 双侧{r.min_wick:.2f}%"
        )

        f = flow_by_date.get(d)
        if not f:
            print(f"■ {d}  {seg_summary}\n   <无资金数据>\n")
            continue
        print(f"■ {d}  收盘 {f.close:.2f}  涨跌 {(f.pct_chg or 0):+.2f}%  |  {seg_summary}")
        print(f"   全天净流入    {(f.net_flow or 0):>+12.0f} 万")
        print(f"   大单净流入    {(f.big_net or 0):>+12.0f} 万  占比 {(f.big_pct or 0):>+6.2f}%  连续 {f.big_consecutive or 0} 日")
        print(f"   中单净流入    {(f.mid_net or 0):>+12.0f} 万  占比 {(f.mid_pct or 0):>+6.2f}%")
        print(f"   小单净流入    {(f.small_net or 0):>+12.0f} 万  占比 {(f.small_pct or 0):>+6.2f}%")
        if f.main_net_5d is not None:
            print(f"   5日主力净额   {f.main_net_5d:>+12.0f} 万")

        nd = _next_trading_day(d)
        if nd and nd.pct_chg is not None:
            print(f"   次日 {nd.date}  涨跌 {nd.pct_chg:>+6.2f}%  收盘 {nd.close:.2f}")
        else:
            print(f"   次日 <无数据>")
        print()
    return 0


def run_smart_money(args: argparse.Namespace) -> int:
    """主力压价吸筹扫描（资金异常流入 + 股价不涨）。

    复用 src.services.smart_money_detector.SmartMoneyDetector：
    - 单股：五阶段详评（吸筹/洗盘/二次吸筹/主升浪/派发）
    - 全量：screen_stealth 隐蔽吸筹筛选（大单流入+股价不动/下跌，按背离度排序）
    """
    logger.info("模式: 主力压价吸筹扫描")
    from src.services.smart_money_detector import (
        SmartMoneyDetector, format_signal, format_stealth_list,
    )

    code = args.smart_money
    detector = SmartMoneyDetector()

    # 单股模式
    if code and code != '__all__':
        code = str(code).zfill(6)
        logger.info("单股分析: %s", code)
        sig = detector.detect(code)
        print(format_signal(sig))
        return 0

    # 全量扫描
    days = max(3, args.days)
    top = max(1, args.top)
    logger.info("全量扫描隐蔽吸筹: days=%d, top=%d", days, top)

    stealth = detector.screen_stealth(days=days, min_inflow_days=3)
    print(format_stealth_list(stealth[:top]))

    # 对 Top 5 再补一份五阶段详评，便于横向对比
    if stealth:
        print()
        print("=" * 70)
        print(f"Top {min(5, len(stealth))} 五阶段详评")
        print("=" * 70)
        for s in stealth[:5]:
            sig = detector.detect(s.code, s.name)
            print()
            print(format_signal(sig))
    return 0


def run_detect_anomaly(args: argparse.Namespace) -> int:
    """压价吸筹异常时段侦测（单股）。

    复用 src.services.anomaly_period_detector.AnomalyPeriodDetector：
    big_net>0 AND small_net<0 AND pct<3% 触发，隔一两天（max_gap=2）算同一时段。
    """
    logger.info("模式: 压价吸筹异常时段侦测")
    from src.services.anomaly_period_detector import (
        AnomalyPeriodDetector, format_periods,
    )
    from src.storage import DatabaseManager, StockDailySyncState

    code = args.detect_anomaly
    if not code:
        logger.error("必须指定 --detect-anomaly CODE")
        return 1
    code = str(code).zfill(6)

    db = DatabaseManager()
    session = db.get_session()
    try:
        row = session.query(StockDailySyncState.code_name).filter(
            StockDailySyncState.code == code,
        ).first()
        name = row[0] if row else '-'
    finally:
        session.close()

    detector = AnomalyPeriodDetector(db=db)
    periods = detector.detect(code, max_gap=args.max_gap)
    print(format_periods(code, name, periods))
    return 0


def run_detect_anomaly_all(args: argparse.Namespace) -> int:
    """全量压价吸筹异常时段聚类。

    扫描所有股票 → 按时段 start_date 聚类 → 反推热门概念。
    """
    logger.info("模式: 全量异常时段聚类")
    from src.services.anomaly_clusterer import AnomalyClusterer, format_clusters

    clusterer = AnomalyClusterer()
    clusters = clusterer.run(
        max_gap=args.max_gap,
        cluster_gap=args.cluster_gap,
        max_span_days=args.max_span_days,
        min_codes=args.min_codes,
    )
    print(format_clusters(clusters))
    return 0


def run_wash_backtest(args: argparse.Namespace) -> int:
    """洗盘选股回测：复现 T 日的 --wash 买入列表，统计 T+1/T+3/T+5 涨幅。"""
    logger.info("模式: 洗盘选股回测")
    from src.services.wash_backtester import WashBacktester, format_backtest

    date_t = args.wash_backtest
    if not date_t or len(date_t) != 8:
        logger.error("--wash-backtest 需要指定 8 位日期 YYYYMMDD")
        return 1

    bt = WashBacktester()
    summary = bt.run(date_t)
    print(format_backtest(summary))
    return 0


def run_wash_v4(args: argparse.Namespace) -> int:
    """洗盘选股回测 V4：在 --wash-backtest 基础上加主升浪形态硬过滤。

    流程：
    1. 复用 PatternScreener（force_refresh）拿到 T 日规律分候选
    2. compute_wash_final 过 score/wash 阈值 → picked（v3 综合分排序）
    3. SurgeScreener.filter_codes_by_surge 对 picked 做 V4 形态硬过滤
       - 通过的进入资金流筛选（buy/observe/avoid）
       - 未通过的进入 rejected_surge 档（保留 pattern 分维度便于对照）
    4. 统计 5 档（buy/observe/avoid/rejected_surge/rejected_v3）的 T+N 表现
    """
    logger.info("模式: 洗盘选股回测 V4 (--wash-v4)")
    from src.services.wash_backtester import WashBacktester, format_backtest
    from src.services.surge_screener import SurgeCriteria

    date_t = args.wash_v4
    if not date_t or len(date_t) != 8:
        logger.error("--wash-v4 需要指定 8 位日期 YYYYMMDD")
        return 1

    logger.info(
        "参数: date=%s, surge_days=%d, ratio_min=%.2f, low_ratio=%.2f, extend_ratio=%.2f, "
        "max_break=%d, min_low_len=%d",
        date_t, args.surge_days, args.surge_ratio_min, args.surge_low_ratio,
        args.surge_extend_ratio, args.surge_max_break_days, args.surge_min_low_len,
    )

    criteria = SurgeCriteria(
        lookback_days=args.surge_days,
        h_l_ratio_min=args.surge_ratio_min,
        low_ratio=args.surge_low_ratio,
        surge_extend_ratio=args.surge_extend_ratio,
        surge_max_break_days=args.surge_max_break_days,
        min_low_group_len=args.surge_min_low_len,
        min_avg_amount_yi=args.surge_min_amount,
        min_h_amount_yi=args.surge_min_h_amount,
    )

    bt = WashBacktester()
    summary = bt.run(date_t, surge_filter=True, surge_criteria=criteria)
    print(format_backtest(summary))
    return 0


def run_find_doji(args: argparse.Namespace) -> int:
    """十字星查询：默认最近交易日全市场；--code 指定股票全历史；--all 全市场全历史。"""
    logger.info("模式: 十字星查询")
    from datetime import datetime as _dt, timedelta as _td
    from src.services.minute_accumulation_detector import MinuteAccumulationDetector
    from src.storage import DatabaseManager, Stock1minKline, StockDailySyncState

    THRESHOLD = 100  # 只返回满分十字星
    db = DatabaseManager()
    detector = MinuteAccumulationDetector()

    def _print_rows(title: str, rows: list) -> None:
        if not rows:
            print(f"\n{title}: 无")
            return
        print(f"\n{title}（共 {len(rows)} 个，满分=100）:\n")
        print(f"{'日期':<12}{'代码':<8}{'名称':<12}{'评分':>4}  {'状态':<14}{'实体%':>7}{'影线%':>6}{'双侧%':>6}")
        print('-' * 80)
        for d, code, name, score, state, body, wick, min_wick in rows:
            name_disp = (name or '-')[:10]
            print(f"{str(d):<12}{code:<8}{name_disp:<12}{score:>4}  {state:<14}"
                  f"{body:>6.2f}%{wick:>5.0f}%{min_wick:>5.2f}%")

    if args.code:
        # 模式 2: 指定股票的所有十字星日期
        code = args.code.zfill(6)
        session = db.get_session()
        try:
            first = session.query(Stock1minKline.ts).filter(
                Stock1minKline.code == code,
            ).order_by(Stock1minKline.ts).first()
            last = session.query(Stock1minKline.ts).filter(
                Stock1minKline.code == code,
            ).order_by(Stock1minKline.ts.desc()).first()
            if not first:
                print(f"{code}: 无 1min 数据")
                return 1
            name_row = session.query(StockDailySyncState.code_name).filter(
                StockDailySyncState.code == code,
            ).first()
            name = name_row[0] if name_row else '-'
        finally:
            session.close()

        print(f"扫描 {code} {name} 从 {first[0].date()} 到 {last[0].date()}", flush=True)
        results = detector.detect(code, start=first[0].date(), end=last[0].date())
        rows = [
            (r.date, code, name, r.score, r.state, r.body_pct, r.wick_ratio, r.min_wick)
            for r in results if r.score >= THRESHOLD
        ]
        rows.sort(key=lambda x: x[0])
        _print_rows(f"{code} {name} 历史十字星", rows)
        return 0

    if args.all:
        # 模式 3: 全市场全历史
        session = db.get_session()
        try:
            codes = [r[0] for r in session.query(Stock1minKline.code).distinct().all()]
            name_map = {
                r[0]: r[1]
                for r in session.query(
                    StockDailySyncState.code, StockDailySyncState.code_name,
                ).all()
            }
        finally:
            session.close()

        print(f"扫描全市场 {len(codes)} 只股票的全部 1min 历史", flush=True)
        rows = []
        for i, code in enumerate(codes):
            results = detector.detect(code, start='2020-01-01', end='2099-12-31')
            for r in results:
                if r.score >= THRESHOLD:
                    rows.append((r.date, code, name_map.get(code, '-'),
                                 r.score, r.state, r.body_pct, r.wick_ratio, r.min_wick))
            if (i + 1) % 20 == 0:
                print(f"  进度 {i+1}/{len(codes)}  累计候选 {len(rows)}", flush=True)
        rows.sort(key=lambda x: (x[1], x[0]))
        _print_rows("全市场历史十字星", rows)
        return 0

    # 模式 1（默认）: 最近交易日全市场 或 --date 指定日
    session = db.get_session()
    try:
        if args.date:
            # 解析 --date YYYYMMDD（或 YYYY-MM-DD）
            try:
                latest_date = _dt.strptime(args.date, "%Y%m%d").date()
            except ValueError:
                latest_date = _dt.strptime(args.date, "%Y-%m-%d").date()
        else:
            latest_row = session.query(Stock1minKline.ts).order_by(Stock1minKline.ts.desc()).first()
            if not latest_row:
                print("无 1min 数据")
                return 1
            latest_date = latest_row[0].date()
        day_start = _dt.combine(latest_date, _dt.min.time())
        day_end = day_start + _td(days=1)
        codes = [r[0] for r in session.query(Stock1minKline.code).filter(
            Stock1minKline.ts >= day_start,
            Stock1minKline.ts < day_end,
        ).distinct().all()]
        name_map = {
            r[0]: r[1]
            for r in session.query(
                StockDailySyncState.code, StockDailySyncState.code_name,
            ).all()
        }
    finally:
        session.close()

    print(f"扫描 {latest_date} 共 {len(codes)} 只股票", flush=True)
    rows = []
    for code in codes:
        results = detector.detect(code, start=latest_date, end=latest_date)
        for r in results:
            if r.score >= THRESHOLD:
                rows.append((r.date, code, name_map.get(code, '-'),
                             r.score, r.state, r.body_pct, r.wick_ratio, r.min_wick))
    rows.sort(key=lambda x: x[3], reverse=True)
    _print_rows(f"{latest_date} 十字星", rows)
    return 0


def run_pattern_screen(args: argparse.Namespace) -> int:
    """规律选股（洗盘精选模式）"""
    logger.info("模式: 规律选股")
    from src.services.pattern_screener import PatternScreener, PatternScreenerConfig

    screener = PatternScreener()
    config_ps = PatternScreenerConfig()
    date_key = args.date
    force_refresh = args.force_refresh
    do_notify = args.notify
    # --wash 和 --wash-v3 都走洗盘精选；只有 --wash 才接资金流分析
    do_wash = args.wash or args.wash_v3
    do_fund_flow_filter = args.wash and not args.wash_v3

    # 概念缓存：由 PatternScreener._load_theme_universe 自带缓存+实时 fallback
    # 这里预先加载一次（激活 fallback），失败直接退出；成功后注入避免重复加载
    actual_date = date_key or screener._get_latest_trading_date() or ""
    if not actual_date:
        logger.error("[PatternScreen] 无法确定交易日（既无 --date 也无 stock_daily 数据）")
        print("无法确定交易日：请用 --date YYYYMMDD 指定，或先运行 --daily-sync 同步数据")
        return 1

    # 概念池加载：统一走 stock_concept_membership DB（已废弃 concept_universe_*.json 路径）
    from src.services.persistent_theme_finder import PersistentThemeFinder
    themes_universe = PersistentThemeFinder.load_themes_from_db()
    if not themes_universe:
        logger.error("[PatternScreen] stock_concept_membership 表为空")
        print("stock_concept_membership 表为空：请先运行 --sync-concept-membership 同步概念成分股数据")
        return 1

    # 数据保障：DB 池是全市场（5300+ 只），stock_daily 已含全量数据，
    # 不需要按需同步；若确实缺数据，请用 --daily-sync 单独跑。
    logger.info(
        "[PatternScreen] DB 概念池：候选 %d 只，依赖 stock_daily 现有数据（如缺数据请 --daily-sync）",
        len(themes_universe),
    )
    daily_report = {"synced": 0, "failed": 0, "rows_written": 0, "skipped": 0, "total": 0}

    # 注入已加载的 themes_universe，避免 screen() 内部重复查 DB
    screener._theme_universe_provider = lambda: themes_universe

    candidates, hot_themes = screener.screen(config_ps, date_key=date_key, force_refresh=force_refresh)
    if not candidates:
        logger.info("无符合条件的股票")
        print("无符合条件的股票")
        return 0

    # 洗盘精选模式
    if do_wash:
        def _wash_final(c):
            return PatternScreener.compute_wash_final(c.score, c.wash_score)

        picked = []
        for c in candidates:
            f = _wash_final(c)
            if f is not None:
                c._wash_final = f
                picked.append(c)
        picked.sort(key=lambda c: -c._wash_final)
        logger.info("洗盘精选完成: %d只股票", len(picked))
    else:
        # 规律选股模式：取倒数5只（规律分最低的5只）
        candidates.sort(key=lambda c: c.score)
        picked = candidates[:5]
        logger.info("规律选股完成: %d只股票", len(picked))

    report = PatternScreener.format_report(
        picked, hot_themes, date_key=actual_date, slim=bool(args.wash_v3)
    )
    print(report)

    # 资金流过滤：洗盘精选后自动接资金流分析（基于4天回测结论的三分类）
    # 流程：洗盘选股 → picked → 补全资金流 → 资金流过滤 → 买入/观察/回避
    # --wash-v3 模式跳过此步，保持纯洗盘输出
    if do_fund_flow_filter and picked:
        # === FundFlowPrefetch（按需版）: 缓存优先，资金流不足才实时抓取 ===
        try:
            from tabulate import tabulate as _tabulate_ff

            picked_codes = [(c.code, c.name) for c in picked]
            ff_prefetch_report = provider.ensure_fund_flow(picked_codes, actual_date)

            # 汇总表：按"是否新抓"分组展示
            new_fetch: List[List[Any]] = []
            skipped: List[List[Any]] = []
            failed: List[List[Any]] = []
            for code, name in picked_codes:
                v = ff_prefetch_report.get(code, -1)
                if v == 0:
                    skipped.append([code, name, "已充足"])
                elif v == -1:
                    failed.append([code, name, "失败"])
                else:
                    new_fetch.append([code, name, f"+{v} 行"])

            if new_fetch:
                print("\n资金流补全(新抓):")
                print(_tabulate_ff(
                    new_fetch, headers=['代码', '名称', '入库'],
                    tablefmt='grid', numalign='right',
                ))
            if skipped:
                print(f"\n资金流已充足跳过: {len(skipped)} 只（{', '.join(r[0] for r in skipped)}）")
            if failed:
                print(f"\n资金流补全失败: {len(failed)} 只（{', '.join(r[0] for r in failed)}）")
        except Exception as exc:
            logger.warning("[FundFlowPrefetch] 整体失败，跳过补全: %s", exc)
        # === FundFlowPrefetch 结束 ===

        try:
            from src.services.fund_flow_screener import FundFlowScreener, format_filter_report

            ff = FundFlowScreener()
            buy_list, observe_list, avoid_list = ff.filter_candidates(picked, date_key=actual_date)
            ff_report = format_filter_report(buy_list, observe_list, avoid_list)
            print(ff_report)
            report += "\n\n" + ff_report
            logger.info(
                "[FundFlowFilter] 过滤完成: 买入=%d 观察=%d 回避=%d",
                len(buy_list), len(observe_list), len(avoid_list),
            )
        except Exception as exc:
            logger.warning("[FundFlowFilter] 过滤失败: %s", exc)

    # 推送到飞书
    if do_notify and picked:
        try:
            from src.notification import NotificationService, NotificationBuilder

            display = ""
            if actual_date and len(actual_date) == 8:
                display = f"{actual_date[:4]}-{actual_date[4:6]}-{actual_date[6:8]}"

            notifier = NotificationService()
            if notifier.is_available():
                title = f"规律选股报告 {display}"
                # 用代码块包裹 report：tabulate grid 不是标准 markdown 表格，
                # 直接发会被飞书 markdown 渲染器破坏（只显示边框，单元格内容丢失）。
                # 代码块让飞书原样显示美化表格。
                alert_text = NotificationBuilder.build_simple_alert(
                    title=title, content=f"```\n{report}\n```", alert_type="info"
                )
                key = f"pattern_screen:{actual_date}"
                notifier.send_with_results(
                    alert_text,
                    route_type="alert",
                    severity="info",
                    dedup_key=key,
                    cooldown_key=key,
                )
                logger.info("[PatternScreen] 已推送到飞书")
            else:
                logger.warning("[PatternScreen] 无可用的通知渠道")
        except Exception as exc:
            logger.warning("[PatternScreen] 推送失败: %s", exc)

    return 0


def run_intraday_screen(args: argparse.Namespace) -> int:
    """盘中选股（独立 tmp 库，同花顺 single_trend 实时分时聚合）。

    守卫：9:30 + 工作日（周末拒绝）。
    算法同 --pattern-screen --wash-v3（纯洗盘精选，无资金流处理）：
    - 历史 N-1 天：从 stock_daily 拷贝到 tmp 库
    - 当日：同花顺 single_trend 抓分时 → 聚合成伪日线 → 写 tmp 库
    - 凭证：INTRADAY_HX_COOKIE / INTRADAY_HX_FUYAO_AUTH（在 .env 配置）
    """
    logger.info("模式: 盘中选股 (--intraday-screen)")
    logger.info(
        "参数: theme_n=%d, max_candidates=%d",
        args.intraday_theme_n, args.intraday_max_candidates,
    )

    # 解析 --intraday-codes（逗号分隔）
    codes = None
    if args.intraday_codes:
        codes = [c.strip() for c in args.intraday_codes.split(",") if c.strip()]
        if not codes:
            logger.error("--intraday-codes 解析后为空")
            print("--intraday-codes 解析后为空，请检查参数格式")
            return 1
        logger.info("[IntradayScreen] 指定股票池模式: %d 只 codes: %s", len(codes), codes)

    from src.services.intraday_screen.pipeline import run_intraday_screen_with_wash

    exit_code, report = run_intraday_screen_with_wash(
        theme_n=args.intraday_theme_n,
        max_candidates=args.intraday_max_candidates,
        codes=codes,
    )
    print(report)
    return exit_code


def run_sideways_screen(args: argparse.Namespace) -> int:
    """横盘选股：找最近 N 天内曾横盘过的股票。

    在 lookback_days 窗口内滑动扫描，找出最长一段满足双维度
    （斜率≈0 + 无连续单边）的区间。输出横盘段起止日期 + 长度。
    不限制价格/均线/振幅/量能。按横盘长度倒序输出。
    """
    logger.info("模式: 横盘选股 (--sideways-screen)")
    logger.info(
        "参数: lookback=%d, min_len>%d, min_amount=%.2f亿, slope_max=%.3f%%/天, max_streak<%d",
        args.sideways_days, args.sideways_min_len, args.sideways_min_amount,
        args.sideways_slope, args.sideways_streak,
    )

    from src.services.sideways_screener import (
        SidewaysCriteria, SidewaysScreener, format_sideways_report,
    )

    criteria = SidewaysCriteria(
        lookback_days=args.sideways_days,
        min_sideways_len=args.sideways_min_len,
        min_avg_amount_yi=args.sideways_min_amount,
        slope_max_pct=args.sideways_slope,
        max_consecutive_days=args.sideways_streak,
    )
    screener = SidewaysScreener()
    candidates = screener.screen(criteria=criteria)
    print(format_sideways_report(candidates))
    return 0


def run_accumulation_screen(args: argparse.Namespace) -> int:
    """资金吸筹选股：量价背离识别。

    最近 N 天每天涨幅 ≤ +3%（下跌不限），且 N 天累计资金净流入 > 0。
    按累计净流入降序输出，含每日明细（涨跌/资金/大单/小单）。
    """
    logger.info("模式: 资金吸筹选股 (--accumulation-screen)")
    logger.info(
        "参数: days=%d, rise_max=%.2f%%, min_amount=%.2f亿",
        args.accumulation_days, args.accumulation_rise_max,
        args.accumulation_min_amount,
    )

    from src.services.fund_flow_accumulation_screener import (
        AccumulationCriteria, FundFlowAccumulationScreener,
        format_accumulation_report,
    )

    criteria = AccumulationCriteria(
        lookback_days=args.accumulation_days,
        daily_rise_max_pct=args.accumulation_rise_max,
        min_avg_amount_yi=args.accumulation_min_amount,
    )
    screener = FundFlowAccumulationScreener()
    candidates = screener.screen(criteria=criteria)
    print(format_accumulation_report(
        candidates,
        lookback_days=criteria.lookback_days,
        daily_rise_max_pct=criteria.daily_rise_max_pct,
    ))
    return 0


def _load_hot_concept_codes(date_key: Optional[str]) -> Tuple[Set[str], Optional[str], List[Tuple[str, int]]]:
    """读取 T 日新增持续热点概念的成分股（与洗盘选股同一基础算法，强制 DB 数据源）。

    底层逻辑：老热点（T-1 已发酵）已被市场消化，真正机会在"今日新冒头"的题材。
    因此缩池模式优先采用 emerging_themes（T − T-1）；无新增时 fallback 到 T 日全集。

    直接通过 PersistentThemeFinder 计算：
      1. find_emerging_themes(): T 日新增持续热点（fallback 到 find()）
      2. theme_universe 属性 + 成分股反查：从 stock_concept_membership 反查命中持续热点的 codes
         （复用 finder 内部已缓存的 universe，避免重复扫 DB）

    不再依赖 concept_universe_*.json（已废弃 --cache-concepts 路径）。

    Args:
        date_key: YYYYMMDD 字符串；None/空 时取数据库最新交易日

    Returns:
        (codes, used_date, hot_themes):
          - codes: 命中持续热点的成分股并集（空集合表示无数据）
          - used_date: 实际使用的交易日（来自数据库）
          - hot_themes: [(theme_name, days), ...] T 日新增持续热点题材列表
    """
    from src.services.persistent_theme_finder import PersistentThemeFinder
    from src.services.pattern_screener import PatternScreener, PatternScreenerConfig

    finder = PersistentThemeFinder()
    config = PatternScreenerConfig()

    # 优先用 T 日新增热点（T 有 T-1 无）；无新增时 fallback 到 T 日全集
    hot_themes = finder.find_emerging_themes(
        lookback_days=config.lookback_days,
        concept_min_days=config.concept_min_days,
        date_key=date_key or "",
    )
    if not hot_themes:
        logger.warning(
            "[SurgeThemeNarrow] T 日无新增持续热点 (date=%s)，fallback 到 T 日全集",
            date_key or "最新",
        )
        hot_themes = finder.find(
            lookback_days=config.lookback_days,
            concept_min_days=config.concept_min_days,
            date_key=date_key or "",
        )

    # 解析实际使用的交易日（用于日志，与 JSON 时代 used_date 含义保持一致）
    used_date = date_key
    if not used_date:
        from src.storage import DatabaseManager, StockDaily
        db = DatabaseManager()
        session = db.get_session()
        try:
            r = session.query(StockDaily.date).order_by(StockDaily.date.desc()).first()
            used_date = r[0].strftime("%Y%m%d") if r else None
        finally:
            session.close()

    if not hot_themes:
        logger.warning("[SurgeThemeNarrow] 无持续热点题材 (date=%s)", used_date or date_key or "最新")
        return set(), used_date, []

    # 反查命中持续热点的成分股（复用 PatternScreener._load_theme_members 的板块黑名单过滤）
    # 复用 finder 内部已缓存的 universe，避免重复扫 40331 条 DB 记录
    screener = PatternScreener()
    theme_universe = finder.theme_universe
    if not theme_universe:
        logger.warning("[SurgeThemeNarrow] stock_concept_membership 为空")
        return set(), used_date, hot_themes
    _, stock_themes, _ = screener._load_theme_members(hot_themes, theme_universe=theme_universe)

    logger.info(
        "[SurgeThemeNarrow] date=%s 持续热点 %d 个: %s → 成分股 %d 只",
        used_date or date_key or "最新", len(hot_themes),
        [t for t, _ in hot_themes], len(stock_themes),
    )
    return set(stock_themes.keys()), used_date, hot_themes


def _enrich_surge_returns(candidates, end_date_str: Optional[str], db=None) -> None:
    """给 SurgeCandidate 列表附加 T+1~T+5 涨跌（原地修改 c.returns）。

    Args:
        candidates: SurgeCandidate 列表
        end_date_str: YYYYMMDD 字符串，None 时查 DB 最新交易日
        db: DatabaseManager，None 时新建
    """
    if not candidates:
        return
    from datetime import datetime
    from src.storage import DatabaseManager, StockDaily

    db = db or DatabaseManager()

    # 解析 T 日
    t_date = None
    if end_date_str:
        try:
            t_date = datetime.strptime(end_date_str.replace("-", ""), "%Y%m%d").date()
        except ValueError:
            pass
    if t_date is None:
        session = db.get_session()
        try:
            r = session.query(StockDaily.date).order_by(StockDaily.date.desc()).first()
            t_date = r[0] if r else None
        finally:
            session.close()
    if t_date is None:
        logger.warning("[SurgeReturns] 无法确定 T 日，跳过 returns enrich")
        return

    session = db.get_session()
    try:
        for c in candidates:
            rows = session.query(StockDaily.date, StockDaily.close).filter(
                StockDaily.code == c.code,
                StockDaily.date >= t_date,
            ).order_by(StockDaily.date.asc()).limit(6).all()

            returns = {}
            if len(rows) >= 1 and rows[0][1] and rows[0][1] > 0:
                close_t = rows[0][1]
                for d in (1, 2, 3, 4, 5):
                    if d < len(rows) and rows[d][1] and rows[d][1] > 0:
                        returns[d] = round((rows[d][1] - close_t) / close_t * 100, 2)
                    else:
                        returns[d] = None
            else:
                for d in (1, 2, 3, 4, 5):
                    returns[d] = None
            c.returns = returns
    finally:
        session.close()

    has_t1 = sum(1 for c in candidates if c.returns.get(1) is not None)
    logger.info("[SurgeReturns] T=%s enrich: %d/%d 只有 T+1 数据", t_date, has_t1, len(candidates))


def run_surge_screen(args: argparse.Namespace) -> int:
    """主升浪选股：基于量能分布识别"低量吸筹洗盘 → 高量主升浪"形态（v3 算法）。

    最近 N 天窗口内：
    - 先剔除 volume 最大的 1 天 + 最小的 1 天（去异常值噪声）
    - 在剩余点找 L = 最低量日，H = 最高量日
    - 低量组 = L 附近 volume ≤ L×low_ratio 的连续日（吸筹洗盘）
    - 高量组 = 低量组结束 → H → H 之后 vol ≥ H×extend_ratio 继续（断点 ≤max_break_days 桥接）
    - 门槛（后置）：高量组均量 / 低量组均量 ≥ ratio_min（默认 1.9）
    - 要求 L 在 H 之前；三层排序：主升天数升序 → 30日高位比例升序 → 倍率降序。
    --date 指定截止日期（窗口右端），默认取数据库最新交易日。
    --surge-theme-narrow 开启时，把当日热点概念成分股作为白名单（缩池）。
    """
    logger.info("模式: 主升浪选股 (--主升浪选股 v3)")
    logger.info(
        "参数: days=%d, ratio_min=%.2f, low_ratio=%.2f, extend_ratio=%.2f, max_break=%d, "
        "min_low_len=%d, min_amount=%.2f亿, min_h_amount=%.2f亿",
        args.surge_days, args.surge_ratio_min, args.surge_low_ratio,
        args.surge_extend_ratio, args.surge_max_break_days,
        args.surge_min_low_len, args.surge_min_amount, args.surge_min_h_amount,
    )

    from src.services.surge_screener import (
        SurgeCriteria, SurgeScreener, format_surge_report,
    )

    # 解析 --surge-codes（逗号分隔）→ 股票池白名单
    codes_whitelist = None
    if args.surge_codes:
        codes_whitelist = {c.strip() for c in args.surge_codes.split(",") if c.strip()}
        if not codes_whitelist:
            logger.error("--surge-codes 解析后为空")
            print("--surge-codes 解析后为空，请检查参数格式")
            return 1
        logger.info("[SurgeScreen] 股票池模式: %d 只 codes: %s", len(codes_whitelist), sorted(codes_whitelist))

    # --surge-theme-narrow：缩池到 T 日持续热点概念成分股（与洗盘选股同算法，强制 DB）
    if args.surge_theme_narrow:
        theme_date = args.date or None  # None → 取数据库最新交易日
        theme_codes, used_date, hot_themes = _load_hot_concept_codes(theme_date)
        if not theme_codes:
            logger.error(
                "持续热点成分股为空 (实际数据日期=%s, 用户传 date=%s)；"
                "请先运行 --sync-concept-membership 同步概念成分股，并确保 stock_daily 有最近15天数据",
                used_date or "无", theme_date or "最新",
            )
            print(
                f"--surge-theme-narrow 失败：无持续热点成分股"
                f"（用户传 date={theme_date or '最新'}，实际数据日期={used_date or '无'}）；"
                f"提示：1) stock_concept_membership 表为空（请 --sync-concept-membership）；"
                f"2) 或最近5交易日无题材连续3天进涨幅top5"
            )
            return 1
        logger.info(
            "[SurgeScreen] 持续热点缩池 数据日期=%s: %d 只成分股（hot_themes=%d 个，用户传 date=%s）",
            used_date, len(theme_codes), len(hot_themes), theme_date or "最新",
        )
        if codes_whitelist:
            # 与 --surge-codes 取交集（最严格精筛）
            intersected = codes_whitelist & theme_codes
            logger.info(
                "[SurgeScreen] 与 --surge-codes 取交集: %d → %d",
                len(codes_whitelist), len(intersected),
            )
            if not intersected:
                logger.warning("[SurgeScreen] 交集为空，--surge-codes 指定的票都不在当日持续热点里")
            codes_whitelist = intersected or codes_whitelist
        else:
            codes_whitelist = set(theme_codes)

    criteria = SurgeCriteria(
        lookback_days=args.surge_days,
        h_l_ratio_min=args.surge_ratio_min,
        low_ratio=args.surge_low_ratio,
        surge_extend_ratio=args.surge_extend_ratio,
        surge_max_break_days=args.surge_max_break_days,
        min_low_group_len=args.surge_min_low_len,
        min_avg_amount_yi=args.surge_min_amount,
        min_h_amount_yi=args.surge_min_h_amount,
        max_candidates=args.surge_max_candidates,
        codes_whitelist=codes_whitelist,
    )
    screener = SurgeScreener(end_date=args.date)
    candidates = screener.screen(criteria=criteria)
    _enrich_surge_returns(candidates, args.date)
    print(format_surge_report(candidates, codes_whitelist=codes_whitelist))
    return 0


def run_schedule_mode(args: argparse.Namespace) -> int:
    """启动定时调度模式"""
    logger.info("模式: 定时任务调度")
    logger.info("调度计划:")
    logger.info("  - 16:30 洗盘选股推送")
    logger.info("  - 08:30 早盘洗盘推送")
    logger.info("  注: 概念缓存/日线/资金流/1min 数据均由 --pattern-screen 按需实时获取，不再预先同步")
    logger.info("  注: 启动时立即补跑一次所有任务，不等定时触发")

    try:
        import schedule
    except ImportError:
        logger.error("schedule 库未安装，请执行: pip install schedule")
        return 1

    # 定义定时任务
    def task_evening_sync_and_screen():
        """16:30 洗盘选股推送（数据由 PatternDataProvider 按需实时获取）"""
        logger.info("=" * 50)
        logger.info("定时任务开始: 洗盘选股推送")
        logger.info("=" * 50)
        try:
            args_evening = argparse.Namespace(
                date=None,
                force_refresh=False,
                pattern_screen=True,
                wash=True,
                wash_v3=False,
                notify=True,
            )
            run_pattern_screen(args_evening)
        except Exception as exc:
            logger.exception("洗盘选股+推送失败: %s", exc)

    def task_morning_screen():
        """08:30 早盘洗盘推送"""
        logger.info("=" * 50)
        logger.info("定时任务开始: 早盘洗盘推送")
        logger.info("=" * 50)
        try:
            args_morning = argparse.Namespace(
                date=None,
                force_refresh=False,
                pattern_screen=True,
                wash=True,
                wash_v3=False,
                notify=True,
            )
            run_pattern_screen(args_morning)
        except Exception as exc:
            logger.exception("早盘洗盘推送失败: %s", exc)

    # 启动飞书 Stream 交互（后台线程，非阻塞）
    feishu_started = False
    try:
        from bot.platforms.feishu_stream import start_feishu_stream_background
        feishu_started = start_feishu_stream_background()
        if feishu_started:
            logger.info("飞书 Stream 交互已启动")
        else:
            logger.info("飞书 Stream 未启用（未配置或 SDK 不可用）")
    except Exception as exc:
        logger.warning("飞书 Stream 启动失败: %s", exc)

    # 注册定时任务（仅洗盘推送；数据获取由 --pattern-screen 内部按需完成）
    schedule.every().day.at("16:30").do(task_evening_sync_and_screen)
    schedule.every().day.at("08:30").do(task_morning_screen)

    logger.info("定时任务已注册，等待执行...")
    logger.info("按 Ctrl+C 退出")

    # 启动补跑：立即同步执行所有定时任务一次，不等 16:30/08:30 触发
    # 用途：进程重启后立刻拿一次最新选股结果并推送，避免错过当天信号
    logger.info("=" * 50)
    logger.info("启动补跑: 立即执行洗盘选股推送 + 早盘洗盘推送")
    logger.info("=" * 50)
    try:
        task_evening_sync_and_screen()
    except Exception as exc:
        logger.exception("启动补跑洗盘推送失败: %s", exc)
    try:
        task_morning_screen()
    except Exception as exc:
        logger.exception("启动补跑早盘推送失败: %s", exc)
    logger.info("启动补跑完成，进入定时主循环")

    # 主循环
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)  # 每30秒检查一次
            
            # 每小时打印一次心跳
            if datetime.now().minute == 0 and datetime.now().second < 30:
                logger.info("调度器运行中... 下次执行: %s", _get_next_run_time(schedule))
    except KeyboardInterrupt:
        logger.info("收到退出信号，程序退出")

    return 0


def _get_next_run_time(schedule_module) -> str:
    """获取下次执行时间"""
    jobs = schedule_module.get_jobs()
    if jobs:
        next_run = min(job.next_run for job in jobs)
        return next_run.strftime('%Y-%m-%d %H:%M:%S')
    return "未设置"


def _start_webui_subprocess() -> None:
    """启动 webui.py 子进程，并注册 atexit 钩子在主进程退出时清理。

    端口/主机由 WEBUI_HOST/WEBUI_PORT（或 API_HOST/API_PORT）环境变量控制，
    默认 127.0.0.1:8000。
    """
    import atexit
    import subprocess

    webui_path = Path(__file__).resolve().parent / "webui.py"
    if not webui_path.exists():
        logger.warning("--with-webui: 找不到 webui.py (%s)，跳过", webui_path)
        return

    host = os.getenv("WEBUI_HOST", os.getenv("API_HOST", "127.0.0.1"))
    port = os.getenv("WEBUI_PORT", os.getenv("API_PORT", "8000"))

    try:
        # 用同一 Python 解释器，保证 venv 一致
        proc = subprocess.Popen(
            [sys.executable, str(webui_path)],
            cwd=str(Path(__file__).resolve().parent),
            # 子进程继承当前环境变量
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        logger.exception("--with-webui: 启动 webui 子进程失败: %s", exc)
        return

    logger.info("--with-webui: webui 子进程已启动 (pid=%s) → http://%s:%s", proc.pid, host, port)

    def _kill_on_exit():
        if proc.poll() is not None:
            return
        try:
            logger.info("--with-webui: 主进程退出，终止 webui 子进程 (pid=%s)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    atexit.register(_kill_on_exit)


def main() -> int:
    """
    主入口函数

    Returns:
        退出码（0 表示成功）
    """
    # 解析命令行参数
    args = parse_arguments()

    # 初始化 bootstrap 日志
    try:
        _setup_bootstrap_logging(debug=args.debug)
    except Exception as exc:
        debug = args.debug if hasattr(args, 'debug') and args.debug else False
        logging.basicConfig(
            level=logging.DEBUG if debug else logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )
        logger.warning("Bootstrap 日志初始化失败，已回退到 stderr: %s", exc)

    # 加载配置
    try:
        config = get_config()
    except Exception as exc:
        logger.exception("加载配置失败: %s", exc)
        return 1

    # 配置日志
    try:
        _setup_runtime_logging(config.log_dir, debug=args.debug)
    except Exception as exc:
        logger.exception("切换到配置日志目录失败: %s", exc)
        return 1

    logger.info("=" * 60)
    logger.info("股票分析系统 启动（精简版）")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # --with-webui：同时拉起 WebUI 子进程
    if args.with_webui:
        _start_webui_subprocess()

    try:
        # 模式1: 增量同步日线
        if args.sync_incremental:
            _kill_duplicate_process('sync-incremental')
            return run_sync_incremental()

        # 模式2.5: 资金流向抓取
        if args.fund_flow:
            _kill_duplicate_process('fund-flow')
            return run_fund_flow_sync(max_stocks=args.max_stocks, force=args.force_refresh)

        # 模式2.6: 概念板块资金流向抓取
        if args.concept_fund_flow:
            _kill_duplicate_process('concept-fund-flow')
            return run_concept_fund_flow_sync(force=args.force_refresh)

        # 模式2.65: 概念板块成分股同步（供后续"股票→题材"反查）
        if args.sync_concept_membership:
            _kill_duplicate_process('sync-concept-membership')
            return run_sync_concept_membership(limit=args.limit)

        # 模式2.8: 1 分钟分时同步（同花顺）
        if args.sync_1min_kline:
            _kill_duplicate_process('sync-1min-kline')
            code_list = _parse_codes(args.sync_code)
            return run_1min_kline_sync(
                max_stocks=args.max_stocks,
                force=args.force_refresh,
                codes=code_list,
            )

        # 模式2.9: 分时吸筹侦测
        if args.detect_accumulation:
            return run_accumulation_detect(args)

        # 模式2.91: 主力压价吸筹扫描（日线资金流维度）
        if args.smart_money:
            _kill_duplicate_process('smart-money')
            return run_smart_money(args)

        # 模式2.911: 压价吸筹异常时段侦测（单股）
        if args.detect_anomaly:
            return run_detect_anomaly(args)

        # 模式2.912: 全量异常时段聚类
        if args.detect_anomaly_all:
            _kill_duplicate_process('detect-anomaly-all')
            return run_detect_anomaly_all(args)

        # 模式2.10: 十字星查询
        if args.find_doji:
            return run_find_doji(args)

        # 模式3: 规律选股+洗盘推送
        if args.pattern_screen:
            _kill_duplicate_process('pattern-screen')
            return run_pattern_screen(args)

        # 模式3.1: 洗盘选股回测
        if args.wash_backtest:
            return run_wash_backtest(args)

        # 模式3.1b: 洗盘选股回测 V4（加主升浪形态过滤）
        if args.wash_v4:
            return run_wash_v4(args)

        # 模式3.2: 盘中选股（实时分时聚合，独立 tmp 库）
        if args.intraday_screen:
            return run_intraday_screen(args)

        # 模式3.3: 横盘选股（双维度判定）
        if args.sideways_screen:
            return run_sideways_screen(args)

        # 模式3.4: 资金吸筹选股（量价背离识别）
        if args.accumulation_screen:
            return run_accumulation_screen(args)

        # 模式3.5: 主升浪选股（量能分布形态识别）
        if args.surge_screen:
            return run_surge_screen(args)

        # 模式4: 定时调度
        if args.schedule:
            _kill_duplicate_process('--schedule')
            return run_schedule_mode(args)

        # 仅指定 --with-webui：主进程保持运行，等待 webui 子进程退出
        if args.with_webui:
            logger.info("--with-webui: 主进程进入等待状态，按 Ctrl+C 退出（将同时终止 webui 子进程）")
            try:
                while True:
                    import time as _t
                    _t.sleep(1.0)
            except KeyboardInterrupt:
                logger.info("用户中断，退出主进程")
                return 130

        # 没有任何模式也没启 webui，显示帮助信息
        logger.warning("未指定任何运行模式，显示帮助信息:")
        _build_parser().print_help()
        return 0

    except KeyboardInterrupt:
        logger.info("用户中断，程序退出")
        return 130
    except Exception as e:
        logger.exception(f"程序执行失败: {e}")
        return 1


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
