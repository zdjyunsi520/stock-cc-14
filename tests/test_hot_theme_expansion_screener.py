# -*- coding: utf-8 -*-
"""Tests for hot-theme expansion screener."""

import json
import tempfile
import unittest
from pathlib import Path
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

    def test_theme_momentum_boosts_accelerating_theme(self):
        snapshot = [
            self._row("600001", "加速A", 3.0, 1.4, 4.0, 90, 3.0),
            self._row("600002", "加速B", 2.0, 1.3, 3.0, 80, 2.0),
            self._row("600003", "加速C", 1.5, 1.2, 2.0, 70, 1.0),
        ]
        universe = {"600001": ("半导体材料",), "600002": ("半导体材料",), "600003": ("半导体材料",)}
        history = {"半导体材料": [{"theme": "半导体材料", "score": 28.0, "active_count": 1, "avg_change_pct": 0.8, "avg_volume_ratio": 0.8}]}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
            theme_history_provider=lambda: history,
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=20))
        theme = result.themes[0]

        self.assertEqual(theme.momentum_stage, "accelerating")
        self.assertEqual(theme.lifecycle_stage, "accelerating")
        self.assertGreater(theme.momentum_score, 0)
        self.assertGreater(theme.lifecycle_score, 0)
        self.assertGreater(theme.score, theme.base_score)
        self.assertTrue(any("热点生命周期确认" in reason for reason in result.candidates[0].reasons))
        self.assertTrue(any("题材曲率确认" in reason for reason in result.candidates[0].reasons))

    def test_theme_lifecycle_marks_climax_as_risk_stage(self):
        snapshot = [
            self._row("600001", "高潮A", 7.2, 2.2, 8.0, 120, 6.0),
            self._row("600002", "高潮B", 6.8, 2.0, 7.0, 100, 5.0),
            self._row("600003", "高潮C", 6.5, 1.8, 6.0, 90, 4.0),
        ]
        universe = {"600001": ("算力",), "600002": ("算力",), "600003": ("算力",)}
        history = {"算力": [{"theme": "算力", "score": 45.0, "active_count": 2, "avg_change_pct": 4.0, "avg_volume_ratio": 1.5}]}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
            theme_history_provider=lambda: history,
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=20))
        theme = result.themes[0]

        self.assertEqual(theme.lifecycle_stage, "climax")
        self.assertGreater(theme.climax_pressure, 0.6)
        self.assertTrue(any("高潮段" in warning for warning in result.candidates[0].warnings))

    def test_theme_momentum_penalizes_fading_theme(self):
        snapshot = [
            self._row("600001", "退潮A", 1.0, 0.8, 4.0, 90, 3.0),
            self._row("600002", "退潮B", -0.5, 0.7, 3.0, 80, 2.0),
            self._row("600003", "退潮C", 0.2, 0.6, 2.0, 70, 1.0),
        ]
        universe = {"600001": ("被动元件",), "600002": ("被动元件",), "600003": ("被动元件",)}
        history = {"被动元件": [{"theme": "被动元件", "score": 60.0, "active_count": 4, "avg_change_pct": 5.0, "avg_volume_ratio": 1.5}]}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
            theme_history_provider=lambda: history,
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=1))
        theme = result.themes[0]

        self.assertEqual(theme.momentum_stage, "fading")
        self.assertIn(theme.lifecycle_stage, {"cooling", "exhausted"})
        self.assertLess(theme.momentum_score, 0)
        self.assertLess(theme.lifecycle_score, 0)
        self.assertLess(theme.score, theme.base_score)

    def test_missing_theme_history_keeps_unknown_momentum(self):
        snapshot = [
            self._row("600001", "无历史A", 2.0, 1.2, 4.0, 80, 2.0),
            self._row("600002", "无历史B", 1.5, 1.1, 3.0, 80, 2.0),
            self._row("600003", "无历史C", 1.0, 1.0, 2.0, 80, 2.0),
        ]
        universe = {"600001": ("新题材",), "600002": ("新题材",), "600003": ("新题材",)}
        screener = HotThemeExpansionScreener(
            snapshot_provider=lambda: snapshot,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_universe_provider=lambda: universe,
            theme_history_provider=lambda: {},
        )

        result = screener.screen(HotThemeExpansionCriteria(min_theme_score=1))

        self.assertEqual(result.themes[0].momentum_stage, "unknown")
        self.assertEqual(result.themes[0].momentum_score, 0)

    def test_archive_history_uses_payload_time_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_dir = Path(tmpdir) / "intraday_pick_archive"
            archive_dir.mkdir(parents=True)
            older = archive_dir / "intraday_pick_older.json"
            newer = archive_dir / "intraday_pick_newer.json"
            older.write_text(json.dumps({
                "generated_at": "2026-06-04 10:20:00",
                "themes": [{"theme": "PCB", "score": 20.0, "active_count": 1}],
            }, ensure_ascii=False), encoding="utf-8")
            newer.write_text(json.dumps({
                "generated_at": "2026-06-04 14:52:00",
                "themes": [{"theme": "PCB", "score": 44.0, "active_count": 2}],
            }, ensure_ascii=False), encoding="utf-8")
            older.touch()

            screener = HotThemeExpansionScreener(
                snapshot_provider=lambda: [
                    self._row("600001", "PCB A", 3.0, 1.3, 4.0, 80, 3.0),
                    self._row("600002", "PCB B", 2.0, 1.2, 3.0, 80, 2.0),
                    self._row("600003", "PCB C", 1.0, 1.1, 2.0, 80, 1.0),
                ],
                daily_provider=self._daily_provider,
                minute_provider=lambda code: self._minute_rows(),
                theme_universe_provider=lambda: {"600001": ("PCB",), "600002": ("PCB",), "600003": ("PCB",)},
                log_dir=tmpdir,
            )

            result = screener.screen(HotThemeExpansionCriteria(min_theme_score=1))

        self.assertGreaterEqual(result.themes[0].score_delta, 0)
        self.assertLess(result.themes[0].score_delta, 20)

    def test_static_fallback_degrades_theme_confidence(self):
        requested = []

        def snapshot_provider(codes):
            requested.extend(codes)
            return [self._row("600183", "静态票", 2.0, 1.2, 3.0, 80, 2.0)]

        manager = type("Manager", (), {
            "get_hot_theme_universe": lambda self: {},
            "get_daily_data": lambda self, code, days: HotThemeExpansionScreenerTests._daily_provider(code, days),
        })()
        screener = HotThemeExpansionScreener(
            snapshot_provider=snapshot_provider,
            daily_provider=self._daily_provider,
            minute_provider=lambda code: self._minute_rows(),
            theme_history_provider=lambda: {"CCL": [{"theme": "CCL", "score": 10, "active_count": 0}]},
        )

        with patch.object(HotThemeExpansionScreener, "_default_manager", return_value=manager):
            result = screener.screen(HotThemeExpansionCriteria(min_theme_score=1))

        self.assertTrue(requested)
        self.assertEqual(result.data_quality["theme_universe_source"], "static_fallback")
        self.assertTrue(result.data_quality["theme_universe_degraded"])
        self.assertEqual(result.themes[0].momentum_stage, "degraded_static_fallback")
        self.assertEqual(result.themes[0].lifecycle_stage, "static_degraded")
        self.assertLessEqual(result.themes[0].score, 32.0)

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
