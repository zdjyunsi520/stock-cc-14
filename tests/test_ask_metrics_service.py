# -*- coding: utf-8 -*-
"""Tests for deterministic ask metrics context."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from src.services.ask_metrics_service import (
    AskMetricsService,
    calculate_minute_metrics,
    calculate_twenty_day_metrics,
    classify_market_bias,
)


class AskMetricsServiceTests(unittest.TestCase):
    def _daily_frame(self, rows: int = 21) -> pd.DataFrame:
        closes = [10 + idx for idx in range(rows)]
        return pd.DataFrame(
            {
                "date": pd.date_range("2026-05-01", periods=rows),
                "open": closes,
                "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes],
                "close": closes,
                "volume": [1000 + idx * 10 for idx in range(rows)],
                "amount": [100000 + idx * 1000 for idx in range(rows)],
                "pct_chg": [0.5] * (rows - 1) + [10.1],
            }
        )

    def test_twenty_day_metrics_are_calculated_from_21_rows(self):
        metrics = calculate_twenty_day_metrics(self._daily_frame())

        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["twenty_day_return_pct"], 200.0)
        self.assertEqual(metrics["limit_up_count_20d"], 1)
        self.assertEqual(metrics["dist_20d_high_pct"], -3.23)
        self.assertEqual(metrics["dist_20d_low_pct"], 200.0)

    def test_twenty_day_metrics_require_enough_daily_rows(self):
        metrics = calculate_twenty_day_metrics(self._daily_frame(rows=10))

        self.assertEqual(metrics["status"], "insufficient_data")

    def test_minute_metrics_cover_intraday_strength_fields(self):
        rows = [
            {"price": 10.0 + idx * 0.01, "avg_price": 9.9 + idx * 0.005}
            for idx in range(40)
        ]

        metrics = calculate_minute_metrics(rows, tail_minutes=10)

        self.assertEqual(metrics["status"], "checked")
        self.assertGreaterEqual(metrics["above_avg_ratio"], 0.9)
        self.assertTrue(metrics["pullback_holds"])
        self.assertTrue(metrics["tail_supports"])

    def test_market_bias_uses_breadth_when_available(self):
        self.assertEqual(classify_market_bias([], {"up_count": 700, "down_count": 300}), "risk_on")
        self.assertEqual(classify_market_bias([], {"up_count": 300, "down_count": 700}), "risk_off")
        self.assertEqual(classify_market_bias([], {"up_count": 500, "down_count": 500}), "mixed")

    def test_context_is_fail_open_when_one_source_fails(self):
        manager = MagicMock()
        manager.get_realtime_quote.side_effect = RuntimeError("quote failed")
        manager.get_daily_data.return_value = (self._daily_frame(rows=30), "unit-test")
        manager.get_fundamental_context.return_value = {"market": "cn", "valuation": {"pe_ratio": 12.3}}
        manager.get_main_indices.return_value = [{"name": "上证指数", "change_pct": 0.5}]
        manager.get_market_stats.return_value = {"up_count": 650, "down_count": 350}
        manager.get_concept_rankings.return_value = ([{"name": "AI", "change_pct": 3.2}], [])
        manager.get_sector_rankings.return_value = ([{"name": "半导体", "change_pct": 2.1}], [])
        manager.get_hot_theme_universe.return_value = {"600519": ["AI"]}
        minute_provider = MagicMock(return_value=[{"price": 10 + idx * 0.01, "avg_price": 9.9} for idx in range(20)])

        context = AskMetricsService(
            data_manager=manager,
            minute_provider=minute_provider,
            trend_analyzer=SimpleNamespace(analyze=lambda _df, code: SimpleNamespace(to_dict=lambda: {"code": code, "trend_status": "uptrend"})),
        ).build("600519")

        self.assertEqual(context["data_quality"]["realtime_quote"], "failed")
        self.assertEqual(context["data_quality"]["daily_data"], "ok")
        self.assertEqual(context["trend"]["trend_status"], "uptrend")
        self.assertEqual(context["hot_themes"]["matched_themes"], ["AI"])
        self.assertEqual(context["market"]["bias"], "risk_on")


if __name__ == "__main__":
    unittest.main()
