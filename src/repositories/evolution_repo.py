# -*- coding: utf-8 -*-
"""File persistence for read-only evolution runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from src.services.evolution_service import EvolutionRun


class EvolutionRepository:
    """Persist evolution run artifacts under a local output directory."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def save_run(self, evolution_run: EvolutionRun) -> Dict[str, str]:
        run_dir = self.output_dir / evolution_run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run_path = run_dir / "evolution_run.json"
        trajectory_path = run_dir / "trajectory.jsonl"
        report_path = run_dir / "summary.md"

        self._write_json(run_path, evolution_run.to_dict())
        self._write_jsonl(trajectory_path, evolution_run.to_trajectory_rows())
        report_path.write_text(self._build_summary(evolution_run), encoding="utf-8")

        return {
            "run_dir": str(run_dir),
            "run": str(run_path),
            "trajectory": str(trajectory_path),
            "summary": str(report_path),
        }

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: list[Dict[str, Any]]) -> None:
        content = "".join(
            json.dumps(row, ensure_ascii=False, default=str) + "\n"
            for row in rows
        )
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _build_summary(evolution_run: EvolutionRun) -> str:
        lines = [
            f"# Evolution Run {evolution_run.run_id}",
            "",
            f"- 状态: {evolution_run.status}",
            f"- 创建时间: {evolution_run.created_at}",
            f"- 样本数: {evolution_run.sample_count}",
            f"- 最小样本数: {evolution_run.min_sample_size}",
            f"- 回看天数: {evolution_run.lookback_days}",
            "",
            "## Steps",
            "",
        ]
        for step in evolution_run.steps:
            lines.extend(
                [
                    f"### {step.step_id}",
                    "",
                    f"- 阶段: {step.stage}",
                    f"- 观察: {step.observed_issue}",
                    f"- 诊断: {step.diagnosis}",
                    f"- 结果: {step.outcome_summary}",
                    "",
                ]
            )
            for candidate in step.candidates:
                gate = candidate.gate
                lines.extend(
                    [
                        f"- 建议: {candidate.suggestion_id}",
                        f"  - 类型: {candidate.suggestion_type}",
                        f"  - 目标: {candidate.target}",
                        f"  - 门禁: {'passed' if gate.passed else 'blocked'} ({', '.join(gate.reasons) or 'ok'})",
                    ]
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
