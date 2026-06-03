# -*- coding: utf-8 -*-
"""Realtime watch worker for market, hotspots, and configured symbols."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from data_provider.base import canonical_stock_code
from src.core.trading_calendar import build_market_phase_context, get_market_for_stock

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_WATCH_INTERVAL_SECONDS = 60
MIN_REALTIME_WATCH_INTERVAL_SECONDS = 30
DEFAULT_REALTIME_WATCH_NOTIFICATION_COOLDOWN_SECONDS = 300
DEFAULT_REALTIME_WATCH_QUOTE_CACHE_TTL_SECONDS = 30
DEFAULT_REALTIME_WATCH_MARKET_CACHE_TTL_SECONDS = 60
DEFAULT_REALTIME_WATCH_HOTSPOT_CACHE_TTL_SECONDS = 120
DEFAULT_REALTIME_WATCH_PRICE_CHANGE_THRESHOLD_PCT = 2.0
DEFAULT_REALTIME_WATCH_VOLUME_RATIO_THRESHOLD = 2.0
DEFAULT_REALTIME_WATCH_MARKET_CHANGE_THRESHOLD_PCT = 0.8
DEFAULT_REALTIME_WATCH_HOTSPOT_CHANGE_THRESHOLD_PCT = 2.0

_MARKET_LABELS = {
    "cn": "A股",
    "hk": "港股",
    "us": "美股",
}


@dataclass
class RealtimeWatchSignal:
    kind: str
    target: str
    title: str
    detail: str
    observed_value: Optional[float] = None
    source: Optional[str] = None

    @property
    def key(self) -> str:
        direction = "flat"
        if self.observed_value is not None:
            direction = "up" if self.observed_value >= 0 else "down"
        return f"{self.kind}:{self.target}:{direction}"


class RealtimeWatchWorker:
    """Run one realtime watch cycle and send a combined alert when signals fire."""

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        notifier: Optional[Any] = None,
        now_provider: Optional[Callable[[], float]] = None,
        quote_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
        market_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
        hotspot_fetcher: Optional[Callable[[int], Dict[str, Any]]] = None,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self.notifier = notifier
        self.now_provider = now_provider or time.time
        self.quote_fetcher = quote_fetcher or self._default_quote_fetcher
        self.market_fetcher = market_fetcher or self._default_market_fetcher
        self.hotspot_fetcher = hotspot_fetcher or self._default_hotspot_fetcher
        self._cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        self._signal_fingerprints: Dict[str, float] = {}

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    @staticmethod
    def _default_quote_fetcher(symbol: str) -> Dict[str, Any]:
        from src.agent.tools.data_tools import _handle_get_realtime_quote

        return _handle_get_realtime_quote(symbol)

    @staticmethod
    def _default_market_fetcher(region: str) -> Dict[str, Any]:
        from src.agent.tools.market_tools import _handle_get_market_indices

        return _handle_get_market_indices(region)

    @staticmethod
    def _default_hotspot_fetcher(top_n: int) -> Dict[str, Any]:
        from src.agent.tools.market_tools import _handle_get_sector_rankings

        return _handle_get_sector_rankings(top_n=top_n)

    def run_once(self) -> Dict[str, int]:
        stats = {
            "symbols": 0,
            "markets": 0,
            "hotspots": 0,
            "signals": 0,
            "notified": 0,
            "skipped": 0,
            "degraded": 0,
            "failed": 0,
        }
        try:
            config = self.config_provider()
        except Exception as exc:
            logger.warning("[RealtimeWatch] Failed to load runtime config: %s", exc)
            stats["failed"] += 1
            return stats

        if not getattr(config, "realtime_watch_enabled", False):
            stats["skipped"] += 1
            return stats

        symbols = self._resolve_symbols(config)
        regions = self._resolve_regions(config, symbols)
        if self._should_skip_for_trading_session(config, regions):
            stats["skipped"] += 1
            return stats

        signals: List[RealtimeWatchSignal] = []
        market_signals, market_degraded = self._collect_market_signals(config, regions)
        signals.extend(market_signals)
        stats["markets"] = len(regions)
        stats["degraded"] += market_degraded

        hotspot_signals, hotspot_degraded = self._collect_hotspot_signals(config)
        signals.extend(hotspot_signals)
        stats["hotspots"] = len(hotspot_signals)
        stats["degraded"] += hotspot_degraded

        symbol_signals, symbol_degraded = self._collect_symbol_signals(config, symbols)
        signals.extend(symbol_signals)
        stats["symbols"] = len(symbols)
        stats["degraded"] += symbol_degraded

        pending = [signal for signal in signals if self._should_notify(signal.key, config)]
        stats["signals"] = len(pending)
        if not pending:
            return stats

        dispatch = self._send_notification(config, pending)
        if getattr(dispatch, "success", False):
            for signal in pending:
                self._mark_notified(signal.key)
            stats["notified"] = 1
        return stats

    def _resolve_symbols(self, config: Any) -> List[str]:
        raw_symbols = getattr(config, "realtime_watch_symbols", None) or getattr(config, "stock_list", []) or []
        seen = set()
        symbols: List[str] = []
        for raw in raw_symbols:
            value = str(raw or "").strip()
            if not value:
                continue
            try:
                code = canonical_stock_code(value)
            except Exception:
                code = value.upper()
            if code not in seen:
                symbols.append(code)
                seen.add(code)
        return symbols

    def _resolve_regions(self, config: Any, symbols: Iterable[str]) -> List[str]:
        raw_region = str(getattr(config, "realtime_watch_market_region", "") or "").strip().lower()
        if not raw_region:
            raw_region = str(getattr(config, "market_review_region", "cn") or "cn").strip().lower()
        if raw_region == "both":
            regions = ["cn", "hk", "us"]
        else:
            regions = [item.strip() for item in raw_region.split(",") if item.strip()]

        for symbol in symbols:
            market = get_market_for_stock(symbol)
            if market and market not in regions:
                regions.append(market)
        return [region for region in regions if region in _MARKET_LABELS]

    def _should_skip_for_trading_session(self, config: Any, regions: List[str]) -> bool:
        if not getattr(config, "realtime_watch_trading_hours_only", True):
            return False
        if not regions:
            return True
        saw_unknown = False
        for region in regions:
            context = build_market_phase_context(
                market=region,
                trigger_source="realtime_watch",
                analysis_intent="intraday_watch",
            )
            if context.is_market_open_now is True:
                return False
            if context.is_market_open_now is None:
                saw_unknown = True
        return not saw_unknown

    def _collect_market_signals(self, config: Any, regions: List[str]) -> Tuple[List[RealtimeWatchSignal], int]:
        if not getattr(config, "realtime_watch_market_enabled", True):
            return [], 0
        threshold = float(getattr(config, "realtime_watch_market_change_threshold_pct", DEFAULT_REALTIME_WATCH_MARKET_CHANGE_THRESHOLD_PCT))
        ttl = int(getattr(config, "realtime_watch_market_cache_ttl_seconds", DEFAULT_REALTIME_WATCH_MARKET_CACHE_TTL_SECONDS))
        signals: List[RealtimeWatchSignal] = []
        degraded = 0
        for region in regions:
            payload = self._cached(("market", region), ttl, lambda region=region: self.market_fetcher(region))
            if not isinstance(payload, dict) or payload.get("error"):
                degraded += 1
                continue
            for item in payload.get("indices") or []:
                change_pct = self._pick_float(item, "change_pct", "pct_chg", "changePercent", "涨跌幅")
                if change_pct is None or abs(change_pct) < threshold:
                    continue
                name = str(item.get("name") or item.get("指数名称") or item.get("code") or "index")
                price = self._pick_float(item, "price", "close", "latest", "最新价")
                detail = f"{_MARKET_LABELS.get(region, region)} {name} 涨跌幅 {change_pct:+.2f}%"
                if price is not None:
                    detail += f"，当前 {price:.2f}"
                signals.append(RealtimeWatchSignal("market", f"{region}:{name}", "大盘异动", detail, change_pct))
        return signals, degraded

    def _collect_hotspot_signals(self, config: Any) -> Tuple[List[RealtimeWatchSignal], int]:
        if not getattr(config, "realtime_watch_hotspots_enabled", True):
            return [], 0
        threshold = float(getattr(config, "realtime_watch_hotspot_change_threshold_pct", DEFAULT_REALTIME_WATCH_HOTSPOT_CHANGE_THRESHOLD_PCT))
        ttl = int(getattr(config, "realtime_watch_hotspot_cache_ttl_seconds", DEFAULT_REALTIME_WATCH_HOTSPOT_CACHE_TTL_SECONDS))
        top_n = int(getattr(config, "realtime_watch_hotspot_top_n", 5) or 5)
        payload = self._cached(("hotspot", str(top_n)), ttl, lambda: self.hotspot_fetcher(top_n))
        if not isinstance(payload, dict) or payload.get("error"):
            return [], 1

        signals: List[RealtimeWatchSignal] = []
        for item in (payload.get("top_sectors") or payload.get("sectors") or [])[:top_n]:
            change_pct = self._pick_float(item, "change_pct", "涨跌幅", "pct_chg")
            if change_pct is None or change_pct < threshold:
                continue
            name = str(item.get("name") or item.get("板块名称") or item.get("sector") or "hotspot")
            signals.append(
                RealtimeWatchSignal(
                    "hotspot",
                    name,
                    "实时热点",
                    f"热点板块 {name} 涨幅 {change_pct:+.2f}%",
                    change_pct,
                )
            )
        return signals, 0

    def _collect_symbol_signals(self, config: Any, symbols: List[str]) -> Tuple[List[RealtimeWatchSignal], int]:
        threshold = float(getattr(config, "realtime_watch_price_change_threshold_pct", DEFAULT_REALTIME_WATCH_PRICE_CHANGE_THRESHOLD_PCT))
        volume_threshold = float(getattr(config, "realtime_watch_volume_ratio_threshold", DEFAULT_REALTIME_WATCH_VOLUME_RATIO_THRESHOLD))
        ttl = int(getattr(config, "realtime_watch_quote_cache_ttl_seconds", DEFAULT_REALTIME_WATCH_QUOTE_CACHE_TTL_SECONDS))
        signals: List[RealtimeWatchSignal] = []
        degraded = 0
        for symbol in symbols:
            quote = self._cached(("quote", symbol), ttl, lambda symbol=symbol: self.quote_fetcher(symbol))
            if not isinstance(quote, dict) or quote.get("error"):
                degraded += 1
                continue
            name = str(quote.get("name") or symbol)
            price = self._safe_float(quote.get("price"))
            change_pct = self._safe_float(quote.get("change_pct"))
            volume_ratio = self._safe_float(quote.get("volume_ratio"))
            source = str(quote.get("source") or "") or None
            if change_pct is not None and abs(change_pct) >= threshold:
                detail = f"{name}({symbol}) 涨跌幅 {change_pct:+.2f}%"
                if price is not None:
                    detail += f"，当前 {price:.2f}"
                signals.append(RealtimeWatchSignal("symbol_price", symbol, "个股价格异动", detail, change_pct, source))
            if volume_ratio is not None and volume_ratio >= volume_threshold:
                detail = f"{name}({symbol}) 量比 {volume_ratio:.2f}，成交明显放大"
                if price is not None:
                    detail += f"，当前 {price:.2f}"
                signals.append(RealtimeWatchSignal("symbol_volume", symbol, "个股量能异动", detail, volume_ratio, source))
        return signals, degraded

    def _send_notification(self, config: Any, signals: List[RealtimeWatchSignal]) -> Any:
        from src.notification import NotificationBuilder, NotificationDispatchResult, NotificationService

        notification_service = self.notifier or NotificationService()
        if not notification_service.is_available():
            return NotificationDispatchResult(dispatched=False, success=False, status="no_channel")

        now_text = datetime.fromtimestamp(self.now_provider()).strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"时间：{now_text}", "", "触发信号："]
        for signal in signals:
            lines.append(f"- {signal.title}: {signal.detail}")
        lines.extend([
            "",
            "说明：这是实时盯盘观察提醒，不代表自动交易指令；是否买卖仍需结合仓位、计划和风险承受能力确认。",
        ])
        content = "\n".join(lines)
        alert_text = NotificationBuilder.build_simple_alert(
            title="实时盯盘提醒",
            content=content,
            alert_type="warning",
        )
        cooldown_key = "realtime_watch:" + ",".join(signal.key for signal in signals)
        return notification_service.send_with_results(
            alert_text,
            route_type="alert",
            severity="warning",
            dedup_key=cooldown_key,
            cooldown_key=cooldown_key,
        )

    def _cached(self, key: Tuple[str, str], ttl_seconds: int, factory: Callable[[], Any]) -> Any:
        now = self.now_provider()
        ttl = max(0, int(ttl_seconds))
        cached = self._cache.get(key)
        if cached is not None and ttl > 0 and now - cached[0] < ttl:
            return cached[1]
        try:
            value = factory()
        except Exception as exc:
            logger.warning("[RealtimeWatch] fetch failed for %s: %s", key, exc)
            value = {"error": str(exc)}
        self._cache[key] = (now, value)
        return value

    def _should_notify(self, signal_key: str, config: Any) -> bool:
        now = self.now_provider()
        cooldown = int(getattr(config, "realtime_watch_notification_cooldown_seconds", DEFAULT_REALTIME_WATCH_NOTIFICATION_COOLDOWN_SECONDS))
        last_seen = self._signal_fingerprints.get(signal_key)
        return last_seen is None or now - last_seen >= max(0, cooldown)

    def _mark_notified(self, signal_key: str) -> None:
        self._signal_fingerprints[signal_key] = self.now_provider()

    @staticmethod
    def _pick_float(data: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            if key in data:
                value = RealtimeWatchWorker._safe_float(data.get(key))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        text = str(value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
