# -*- coding: utf-8 -*-
"""Tests for hot-theme expansion screener."""

import unittest
from unittest.mock import patch

import pandas as pd

from src.services.hot_theme_expansion_screener import (
    HotThemeExpansionCriteria,
    HotThemeExpansionScreener,
)


class HotThemeExpansionScreenerTests(unittest.TestCase):
    def test_scores_expanding_theme_and_laggard_candidate_with_minute_confirmation(self):
        snapshot = [
            self._row("600001", "核心趋势", 4.0, 1.4, 5.0, 120, 4.0),
            self._row("600002", "补涨观察", 1.8, 1.3, 4.0, 80, 2.4),
            self._row("600003", "扩散成员", 2.2, 1.2, 3.5, 90, 2.1),
        ]
        universe = {"600001": ("PCB",), "600002": ("PCB",), "600003": ("PCB",)}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=20))

        self.assertEqual(result.themes[0].theme, "PCB")
        self.assertEqual(result.themes[0].stage, "扩散")
        ranked_codes = [candidate.code for candidate in result.candidates]
        self.assertLess(ranked_codes.index("600002"), ranked_codes.index("600001"))
        self.assertLess(ranked_codes.index("600003"), ranked_codes.index("600001"))
        self.assertTrue(any("低位补涨观察区" in reason for reason in result.candidates[0].reasons))
        self.assertTrue(any("分时均线上方占比" in reason for reason in result.candidates[0].reasons))

    def test_empty_snapshot_marks_result_data_quality_unusable(self):
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: [],
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: {"600001": ("PCB",)},
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=20))

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.data_quality["snapshot_count"], 0)
        self.assertEqual(result.data_quality["snapshot_status"], "source_failed")
        self.assertEqual(result.data_quality["snapshot_error"], "realtime_snapshot_source_failed")

    def test_default_universe_comes_from_market_hot_boards(self):
        requested = []

        def snapshot_provider(codes):
            requested.extend(codes)
            return [self._row("600001", "题材票", 2.0, 1.2, 3.0, 80, 2.0)]

        manager = type("Manager", (), {
            "get_hot_theme_universe": lambda self: {"600001": ["当日热点"], "600002": ["当日热点"]},
            "get_daily_data": lambda self, code, days: HotThemeExpansionScreenerTests._daily_provider(code, days),
        })()
        screener = HotThemeExpansionScreener(
            snapshot_provider=snapshot_provider,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
        )

        with patch.object(HotThemeExpansionScreener, "_default_manager", return_value=manager):
            result = screener.screen(HotThemeExpansionCriteria(min_theme_score=1))

        self.assertEqual(requested, ["600001", "600002"])
        self.assertEqual(result.data_quality["theme_universe_source"], "market_hot_board")

    def test_overheated_candidate_is_penalized_below_laggard(self):
        snapshot = [
            self._row("600001", "过热票", 9.2, 1.4, 7.0, 100, 8.0),
            self._row("600002", "补涨票", 2.0, 1.2, 4.0, 90, 2.6),
            self._row("600003", "扩散票", 1.2, 1.1, 3.0, 90, 2.0),
        ]
        universe = {"600001": ("CCL",), "600002": ("CCL",), "600003": ("CCL",)}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=20))
        ranked_codes = [candidate.code for candidate in result.candidates]
        overheated = next(candidate for candidate in result.candidates if candidate.code == "600001")

        self.assertLess(ranked_codes.index("600002"), ranked_codes.index("600001"))
        self.assertTrue(any("已过热" in warning for warning in overheated.warnings))

    @staticmethod
    def _row(code, name, change_pct, volume_ratio, turnover_rate, circ_mv_yi, amount_yi):
        return {
            "code": code,
            "name": name,
            "price": 10.0,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "turnover_rate": turnover_rate,
            "circ_mv": circ_mv_yi * 100_000_000,
            "amount": amount_yi * 100_000_000,
        }

    @staticmethod
    def _daily_provider(code, days):
        closes = [10 + idx * 0.1 for idx in range(40)]
        return pd.DataFrame({"date": pd.date_range("2026-04-01", periods=40), "close": closes, "pct_chg": [0.5] * 40}), "test"

    @staticmethod
    def _minute_rows():
        return [
            {"price": 10.0 + idx * 0.02, "avg_price": 10.0 + idx * 0.01}
            for idx in range(40)
        ]


if __name__ == "__main__":
    unittest.main()
