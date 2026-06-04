# -*- coding: utf-8 -*-
"""Tests for read-only evolution diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config
from src.repositories.evolution_repo import EvolutionRepository
from src.services.evolution_service import EvolutionService


@dataclass
class _HistoryRecord:
    context_snapshot: str = ""
    analysis_summary: str = "summary"


class EvolutionServiceTestCase(unittest.TestCase):
    def _config(self) -> Config:
        return Config(
            evolution_enabled=False,
            evolution_lookback_days=30,
            evolution_min_sample_size=3,
            evolution_output_dir="reports/evolution-test",
        )

    def _service(self, records, summary=None) -> EvolutionService:
        return EvolutionService(
            self._config(),
            history_provider=lambda _days, _limit: list(records),
            backtest_summary_provider=lambda: summary,
            now_provider=lambda: datetime(2026, 6, 4, 9, 30, tzinfo=timezone.utc),
        )

    def test_run_dry_run_blocks_when_sample_size_is_insufficient(self) -> None:
        service = self._service([_HistoryRecord()])

        run = service.run_dry_run(min_sample_size=3)

        self.assertEqual(run.status, "insufficient_data")
        self.assertEqual(run.sample_count, 1)
        self.assertEqual(run.steps[0].step_id, "sample_gate")
        self.assertFalse(run.steps[0].candidates[0].gate.passed)
        self.assertIn("insufficient_sample_size", run.steps[0].candidates[0].gate.reasons)

    def test_static_theme_fallback_is_blocked_as_hotspot_fact(self) -> None:
        records = [
            _HistoryRecord(
                context_snapshot=json.dumps(
                    {
                        "screening": {
                            "data_quality": {
                                "theme_universe_source": "static_fallback",
                            }
                        }
                    }
                )
            )
            for _ in range(3)
        ]
        service = self._service(records)

        run = service.run_dry_run(min_sample_size=3)

        hotspot_steps = [step for step in run.steps if step.step_id == "hotspot_fact_gate"]
        self.assertEqual(len(hotspot_steps), 1)
        gate = hotspot_steps[0].candidates[0].gate
        self.assertFalse(gate.passed)
        self.assertIn("static_theme_fallback_cannot_confirm_hotspot", gate.reasons)

    def test_repository_writes_run_trajectory_and_summary(self) -> None:
        service = self._service([_HistoryRecord() for _ in range(3)])
        run = service.run_dry_run(min_sample_size=3)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = EvolutionRepository(tmpdir).save_run(run)

            self.assertTrue(Path(paths["run"]).exists())
            self.assertTrue(Path(paths["trajectory"]).exists())
            self.assertTrue(Path(paths["summary"]).exists())
            payload = json.loads(Path(paths["run"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["sample_count"], 3)
            rows = Path(paths["trajectory"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), len(run.steps))

    def test_low_direction_accuracy_generates_prompt_candidate(self) -> None:
        records = [_HistoryRecord() for _ in range(3)]
        service = self._service(
            records,
            summary={
                "total_evaluations": 3,
                "completed_count": 3,
                "insufficient_count": 0,
                "direction_accuracy_pct": 33.3,
            },
        )

        run = service.run_dry_run(min_sample_size=3)

        direction_steps = [step for step in run.steps if step.step_id == "direction_accuracy"]
        self.assertEqual(len(direction_steps), 1)
        self.assertEqual(direction_steps[0].candidates[0].suggestion_type, "prompt_fragment")


if __name__ == "__main__":
    unittest.main()
