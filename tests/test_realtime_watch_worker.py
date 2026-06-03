# -*- coding: utf-8 -*-
"""Tests for realtime watch worker."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.notification import NotificationDispatchResult
from src.services.realtime_watch_worker import RealtimeWatchWorker


class _Notifier:
    def __init__(self) -> None:
        self.sent = []

    def is_available(self) -> bool:
        return True

    def send_with_results(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return NotificationDispatchResult(dispatched=True, success=True, status="sent")


class RealtimeWatchWorkerTestCase(unittest.TestCase):
    def _config(self, **overrides):
        defaults = {
            "realtime_watch_enabled": True,
            "realtime_watch_symbols": ["601799"],
            "stock_list": ["600519"],
            "realtime_watch_market_enabled": True,
            "realtime_watch_hotspots_enabled": True,
            "realtime_watch_market_region": "cn",
            "market_review_region": "cn",
            "realtime_watch_trading_hours_only": False,
            "realtime_watch_price_change_threshold_pct": 2.0,
            "realtime_watch_volume_ratio_threshold": 2.0,
            "realtime_watch_market_change_threshold_pct": 0.8,
            "realtime_watch_hotspot_change_threshold_pct": 2.0,
            "realtime_watch_hotspot_top_n": 5,
            "realtime_watch_quote_cache_ttl_seconds": 30,
            "realtime_watch_market_cache_ttl_seconds": 60,
            "realtime_watch_hotspot_cache_ttl_seconds": 120,
            "realtime_watch_notification_cooldown_seconds": 300,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_run_once_combines_market_hotspot_and_symbol_signals(self) -> None:
        notifier = _Notifier()
        worker = RealtimeWatchWorker(
            config_provider=lambda: self._config(),
            notifier=notifier,
            now_provider=lambda: 1000.0,
            quote_fetcher=lambda symbol: {
                "code": symbol,
                "name": "星宇股份",
                "price": 126.5,
                "change_pct": -2.5,
                "volume_ratio": 2.3,
                "source": "tencent",
            },
            market_fetcher=lambda region: {
                "indices": [{"name": "上证指数", "price": 3100.0, "change_pct": -0.9}],
            },
            hotspot_fetcher=lambda top_n: {
                "top_sectors": [{"name": "汽车零部件", "change_pct": 2.6}],
            },
        )

        stats = worker.run_once()

        self.assertEqual(stats["symbols"], 1)
        self.assertEqual(stats["markets"], 1)
        self.assertEqual(stats["signals"], 4)
        self.assertEqual(stats["notified"], 1)
        self.assertEqual(len(notifier.sent), 1)
        content, kwargs = notifier.sent[0]
        self.assertIn("大盘异动", content)
        self.assertIn("实时热点", content)
        self.assertIn("个股价格异动", content)
        self.assertIn("个股量能异动", content)
        self.assertEqual(kwargs["route_type"], "alert")

    def test_signal_cooldown_suppresses_repeated_notifications(self) -> None:
        now = {"value": 1000.0}
        notifier = _Notifier()
        worker = RealtimeWatchWorker(
            config_provider=lambda: self._config(),
            notifier=notifier,
            now_provider=lambda: now["value"],
            quote_fetcher=lambda symbol: {"code": symbol, "name": symbol, "change_pct": 2.5},
            market_fetcher=lambda region: {"indices": []},
            hotspot_fetcher=lambda top_n: {"top_sectors": []},
        )

        first = worker.run_once()
        second = worker.run_once()
        now["value"] += 301
        third = worker.run_once()

        self.assertEqual(first["notified"], 1)
        self.assertEqual(second["signals"], 0)
        self.assertEqual(third["notified"], 1)
        self.assertEqual(len(notifier.sent), 2)

    def test_trading_hours_gate_skips_when_all_markets_are_closed(self) -> None:
        worker = RealtimeWatchWorker(
            config_provider=lambda: self._config(realtime_watch_trading_hours_only=True),
            notifier=_Notifier(),
        )
        closed_context = SimpleNamespace(is_market_open_now=False)

        with patch(
            "src.services.realtime_watch_worker.build_market_phase_context",
            return_value=closed_context,
        ):
            stats = worker.run_once()

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["signals"], 0)


if __name__ == "__main__":
    unittest.main()
