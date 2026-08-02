# -*- coding: utf-8 -*-
"""同花顺 stockpage 扶摇 single_trend 接口聚合为伪日线。

接口：POST https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1/single_trend
返回：分时累计数据，单根 = [ts_ms, price, cum_vol, cum_amount]；base_price=昨收

凭证通过环境变量读取（避免硬编码）：
- INTRADAY_HX_COOKIE：cURL 里的 -b '...' 整段 cookie
- INTRADAY_HX_FUYAO_AUTH：cURL 里的 x-fuyao-auth 头（JWT license token，长期有效）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


TREND_URL = "https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1/single_trend"

# x-fuyao-auth 是 license JWT，不与用户 session 绑定（不轻易过期）。
# 这里仅作 fallback；推荐用环境变量 INTRADAY_HX_FUYAO_AUTH 覆盖。
_DEFAULT_FUYAO_AUTH = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJhdXRob3JpemVyX25hbWVzcGFjZSI6ImNvbW1vbi1ocS1hZ2dyIiwibGljZW5zZWVfdHlwZSI6IkZST05UX0FQUCIsImxpY2Vuc2VlX25hbWVzcGFjZSI6Imh4a2xpbmUtTkVXU19hcHBOZXdzRmxvd0hvbWVfUGFnZSJ9."
    "ldrvWTheNnGOa_rH_buA6OoUpLtW2bhcdr3fABrGHbk"
)

_FIXED_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://stockpage.10jqka.com.cn",
    "Referer": "https://stockpage.10jqka.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "platform": "hxkline",
    "source-id": "hxkline-NEWS_appNewsFlowHome_Page",
    "x-auth-appname": "AINVEST",
    "x-auth-progid": "7047",
    "x-auth-type": "ths",
    "x-auth-version": "1.0",
}


def _resolve_market(code: str) -> str:
    """6 位代码 → 扶摇 market 字段：33=深市，17=沪市。

    候选股已在 intraday_realtime_concepts 过滤掉 300/301/688/4/8 开头。
    """
    if code.startswith(("600", "601", "603", "605")):
        return "17"
    return "33"


def _resolve_credentials() -> Tuple[str, str]:
    """从 .env / 系统环境变量取 cookie + x-fuyao-auth。缺失时抛清晰错误。

    项目其他模块用 dotenv_values 直接读 .env，此处沿用相同模式。
    """
    cookie = os.environ.get("INTRADAY_HX_COOKIE", "").strip()
    auth = os.environ.get("INTRADAY_HX_FUYAO_AUTH", "").strip()

    if not cookie or not auth:
        # fallback: 直接从项目根 .env 读（与 src/config.py 一致）
        from pathlib import Path
        from dotenv import dotenv_values
        env_path = Path(os.environ.get("ENV_FILE") or Path(__file__).resolve().parents[3] / ".env")
        if env_path.exists():
            vals = dotenv_values(env_path)
            cookie = cookie or (vals.get("INTRADAY_HX_COOKIE") or "").strip()
            auth = auth or (vals.get("INTRADAY_HX_FUYAO_AUTH") or "").strip()

    if not cookie:
        raise RuntimeError(
            "[MinuteAggregator] 未配置 INTRADAY_HX_COOKIE。"
            "请在 .env 设置（值=同花顺 stockpage 页面请求里的 Cookie 头整段）"
        )
    auth = auth or _DEFAULT_FUYAO_AUTH
    return cookie, auth


def fetch_trend(code: str) -> Optional[dict]:
    """调 single_trend 拿 code 的当日分时数据。

    Returns:
        dict: {base_price, points: [[ts_ms, price, cum_vol, cum_amount], ...]}
        失败返回 None
    """
    cookie, fuyao_auth = _resolve_credentials()
    market = _resolve_market(code)

    body = json.dumps({
        "code_list": [{"codes": [code], "market": market}],
        "trade_date": 0,
        "gpid": 0,
        "time_zone": "Asia/Shanghai",
        "trade_class": "intraday",
    }).encode("utf-8")

    headers = dict(_FIXED_HEADERS)
    headers["Cookie"] = cookie
    headers["x-fuyao-auth"] = fuyao_auth

    req = urllib.request.Request(TREND_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[MinuteAggregator] %s single_trend 请求失败: %s", code, exc)
        return None

    if data.get("status_code") != 0:
        logger.warning(
            "[MinuteAggregator] %s single_trend 状态异常: %s",
            code, data.get("status_msg"),
        )
        return None

    quote_data = (data.get("data") or {}).get("quote_data") or []
    if not quote_data:
        logger.warning("[MinuteAggregator] %s single_trend 返回空 quote_data", code)
        return None

    q = quote_data[0]
    return {
        "base_price": float(q.get("base_price") or 0.0),
        "points": [list(map(float, row)) for row in (q.get("value") or [])],
    }


def aggregate_trend_to_db(
    engine: Engine, code: str, today: date, trend: dict
) -> bool:
    """把 fetch_trend 的结果聚合成伪日线 upsert 到 tmp 库。

    聚合规则（同花顺 single_trend 返回的是分时累计值）：
    - open  = 第一根 price
    - close = 最后一根 price
    - high  = max(所有 price)
    - low   = min(所有 price)
    - volume = 最后一根 cum_vol（全日累计成交量，单位股）
    - amount = 最后一根 cum_amount（全日累计成交额，单位元）
    - pct_chg = (close / base_price - 1) * 100（base_price=昨收）
    """
    points: List[List[float]] = trend.get("points") or []
    if not points:
        return False

    prices = [p[1] for p in points]
    open_p = prices[0]
    close_p = prices[-1]
    high_p = max(prices)
    low_p = min(prices)
    # cum 末值即全日总量
    volume = points[-1][2] if len(points[-1]) > 2 else 0.0
    amount = points[-1][3] if len(points[-1]) > 3 else 0.0

    base_price = trend.get("base_price") or 0.0
    pct_chg = (close_p / base_price - 1) * 100 if base_price > 0 else 0.0

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT OR REPLACE INTO stock_daily_intraday_tmp
                (code, date, open, high, low, close, volume, amount, pct_chg)
                VALUES (:code, :date, :open, :high, :low, :close, :volume, :amount, :pct_chg)
            """),
            {
                "code": code,
                "date": today.isoformat(),
                "open": open_p, "high": high_p, "low": low_p, "close": close_p,
                "volume": volume, "amount": amount, "pct_chg": pct_chg,
            },
        )
    return True


def aggregate_today_from_1min(
    engine: Engine, code: str, today: date
) -> bool:
    """抓 code 当日分时并写入 tmp 库（fetch_trend + aggregate_trend_to_db 组合）。

    Returns:
        True 表示成功抓到并写入 today 数据
    """
    trend = fetch_trend(code)
    if not trend or not trend.get("points"):
        logger.debug("[MinuteAggregator] %s 当日分时数据为空", code)
        return False
    return aggregate_trend_to_db(engine, code, today, trend)
