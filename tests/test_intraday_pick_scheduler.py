# -*- coding: utf-8 -*-
"""Tests for intraday pick scheduler report rendering."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.services.hot_theme_expansion_screener import HotThemeExpansionCandidate, HotThemeExpansionCriteria
from src.services import intraday_pick_scheduler
from src.services.intraday_pick_scheduler import IntradayPickScheduler


class IntradayPickSchedulerReportTests(unittest.TestCase):
    def test_fallback_report_renders_hot_theme_expansion_payload(self):
        payload = {
            "title": "热点扩散盘中选股",
            "summary": {"rough_count": 3, "checked_count": 1, "passed_count": 1, "theme_count": 1, "active_theme_count": 1},
            "themes": [{
                "theme": "CCL",
                "stage": "启动",
                "score": 50.0,
                "member_count": 3,
                "up_count": 2,
                "active_count": 2,
                "reasons": ["放量上涨扩散 2 只"],
            }],
            "ranked_candidates": [{
                "code": "002636",
                "name": "金安国纪",
                "themes": ["CCL", "覆铜板"],
                "reliability_score": 84.5,
                "theme_score": 50.0,
                "change_pct": 1.8,
                "volume_ratio": 1.26,
                "turnover_rate": 5.33,
                "passed": True,
                "reliability_reasons": ["热点扩散分 50.0", "涨幅 1.80% 处于低位补涨观察区"],
                "rejected_reasons": [],
            }],
        }

        report = IntradayPickScheduler._build_fallback_report(payload)

        self.assertIn("热点扩散盘中选股", report)
        self.assertIn("活跃题材：1/1", report)
        self.assertIn("CCL：启动", report)
        self.assertIn("002636 金安国纪（CCL/覆铜板）", report)
        self.assertIn("低位补涨优先", report)

    def test_fallback_report_marks_empty_snapshot_unusable(self):
        payload = {
            "title": "热点扩散盘中选股",
            "summary": {
                "rough_count": 0,
                "checked_count": 0,
                "passed_count": 0,
                "theme_count": 0,
                "active_theme_count": 0,
                "data_quality": {"snapshot_count": 0, "snapshot_status": "empty"},
            },
            "themes": [],
            "ranked_candidates": [],
        }

        report = IntradayPickScheduler._build_fallback_report(payload)

        self.assertIn("实时行情快照为空", report)
        self.assertIn("本次盘中选股不可用", report)
        self.assertIn("候选列表无效", report)

    def test_archive_payload_writes_complete_candidates_to_log_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "slot": "10:20",
                "generated_at": "2026-06-02 10:20:00",
                "ranked_candidates": [
                    {"code": "600001", "reliability_score": 80},
                    {"code": "600002", "reliability_score": 70},
                ],
            }

            archive_path = IntradayPickScheduler._archive_payload(SimpleNamespace(log_dir=tmpdir), payload)

            self.assertTrue(archive_path)
            archive_file = Path(archive_path)
            self.assertEqual(archive_file.parent.name, "intraday_pick_archive")
            archived = json.loads(archive_file.read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in archived["ranked_candidates"]], ["600001", "600002"])

    def test_payload_keeps_report_limit_separate_from_archive_candidates(self):
        result = SimpleNamespace(
            data_quality={"matched_snapshot_count": 12, "active_theme_count": 1},
            criteria=HotThemeExpansionCriteria(max_candidates=20),
            themes=[],
        )
        ranked = [self._candidate(f"6000{idx:02d}", idx) for idx in range(12)]

        archive_payload = IntradayPickScheduler._build_payload(
            SimpleNamespace(intraday_pick_dry_run=True),
            "10:20",
            datetime(2026, 6, 2, 10, 20),
            result,
            ranked,
        )
        report_payload = {**archive_payload, "ranked_candidates": archive_payload["ranked_candidates"][:10]}

        self.assertEqual(len(archive_payload["ranked_candidates"]), 12)
        self.assertEqual(len(report_payload["ranked_candidates"]), 10)
        self.assertEqual(report_payload["ranked_candidates"][-1]["code"], "600009")

    def test_data_source_failure_retries_same_slot_after_ten_minutes(self):
        clock = {"value": 1000.0}
        calls = {"count": 0}

        def failing_screen(criteria):
            calls["count"] += 1
            return SimpleNamespace(
                candidates=[],
                data_quality={
                    "snapshot_count": 0,
                    "snapshot_status": "source_failed",
                    "snapshot_error": "realtime_snapshot_source_failed",
                    "matched_snapshot_count": 0,
                    "active_theme_count": 0,
                },
                criteria=criteria,
                themes=[],
            )

        scheduler = IntradayPickScheduler(
            config_provider=lambda: SimpleNamespace(
                intraday_pick_enabled=True,
                intraday_pick_trading_hours_only=False,
                intraday_pick_push_times=["10:20"],
                intraday_pick_time_window_minutes=3,
                intraday_pick_dry_run=True,
                intraday_pick_report_limit=10,
            ),
            now_provider=lambda: datetime(2026, 6, 2, 10, 20),
            clock_provider=lambda: clock["value"],
        )

        original_screen = intraday_pick_scheduler.screen_hot_theme_expansion
        try:
            intraday_pick_scheduler.screen_hot_theme_expansion = failing_screen
            first = scheduler.run_once()
            second = scheduler.run_once()
            clock["value"] += 600
            third = scheduler.run_once()
        finally:
            intraday_pick_scheduler.screen_hot_theme_expansion = original_screen

        self.assertEqual(first["retry_after_seconds"], 600)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(third["retry_after_seconds"], 600)
        self.assertEqual(calls["count"], 2)

    @staticmethod
    def _candidate(code, score):
        return HotThemeExpansionCandidate(
            code=code,
            name=f"候选{score}",
            themes=["PCB"],
            score=float(score),
            theme_score=50.0,
            price=10.0,
            change_pct=3.5,
            volume_ratio=1.2,
            turnover_rate=1.0,
            circ_mv=10_000_000_000.0,
            amount=200_000_000.0,
            laggard_priority=1 if score % 2 == 0 else 0,
            reasons=["测试理由"],
            warnings=[],
            metrics={},
            data_quality={},
        )


if __name__ == "__main__":
    unittest.main()
