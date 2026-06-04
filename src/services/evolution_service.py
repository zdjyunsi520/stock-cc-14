# -*- coding: utf-8 -*-
"""Read-only self-evolution diagnostics for analysis quality."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.config import Config, get_config


SuggestionType = str


@dataclass
class EvolutionGateResult:
    gate_name: str
    passed: bool
    sample_size: int
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateSuggestion:
    suggestion_id: str
    suggestion_type: SuggestionType
    target: str
    rationale: str
    proposed_change: Dict[str, Any]
    rollback: str
    gate: EvolutionGateResult

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["gate"] = self.gate.to_dict()
        return payload


@dataclass
class EvolutionStep:
    step_id: str
    stage: str
    observed_issue: str
    diagnosis: str
    candidates: List[CandidateSuggestion] = field(default_factory=list)
    source_samples: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    outcome_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return payload


@dataclass
class EvolutionRun:
    run_id: str
    created_at: str
    lookback_days: int
    min_sample_size: int
    sample_count: int
    status: str
    config_summary: Dict[str, Any]
    steps: List[EvolutionStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload

    def to_trajectory_rows(self) -> List[Dict[str, Any]]:
        return [
            {
                "run_id": self.run_id,
                "created_at": self.created_at,
                "step_id": step.step_id,
                "stage": step.stage,
                "before_state": {
                    "lookback_days": self.lookback_days,
                    "sample_count": self.sample_count,
                    "config": self.config_summary,
                },
                "observed_issue": step.observed_issue,
                "diagnosis": step.diagnosis,
                "candidates": [candidate.to_dict() for candidate in step.candidates],
                "gate_results": [candidate.gate.to_dict() for candidate in step.candidates],
                "metrics": step.metrics,
                "outcome_summary": step.outcome_summary,
            }
            for step in self.steps
        ]


class EvolutionService:
    """Build a dry-run evolution report from existing analysis evidence."""

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        history_provider: Optional[Callable[[int, int], List[Any]]] = None,
        backtest_summary_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or get_config()
        self.history_provider = history_provider or self._load_history
        self.backtest_summary_provider = backtest_summary_provider or self._load_backtest_summary
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def run_dry_run(
        self,
        *,
        lookback_days: Optional[int] = None,
        min_sample_size: Optional[int] = None,
    ) -> EvolutionRun:
        lookback = int(lookback_days or getattr(self.config, "evolution_lookback_days", 30))
        min_samples = int(min_sample_size or getattr(self.config, "evolution_min_sample_size", 20))
        records = list(self.history_provider(lookback, max(min_samples * 5, 100)))
        sample_count = len(records)
        summary = self.backtest_summary_provider() or {}
        now = self.now_provider()

        steps: List[EvolutionStep] = []
        steps.append(self._build_sample_gate_step(sample_count, min_samples, lookback))
        steps.extend(self._build_history_quality_steps(records, min_samples))
        steps.extend(self._build_backtest_steps(summary, sample_count, min_samples))

        status = "insufficient_data" if sample_count < min_samples else "completed"
        return EvolutionRun(
            run_id=f"evo-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            created_at=now.isoformat(),
            lookback_days=lookback,
            min_sample_size=min_samples,
            sample_count=sample_count,
            status=status,
            config_summary={
                "evolution_enabled": bool(getattr(self.config, "evolution_enabled", False)),
                "output_dir": str(getattr(self.config, "evolution_output_dir", "reports/evolution")),
                "mode": "dry_run",
            },
            steps=steps,
        )

    def _load_history(self, lookback_days: int, limit: int) -> List[Any]:
        from src.repositories.analysis_repo import AnalysisRepository

        return AnalysisRepository().get_list(days=lookback_days, limit=limit)

    def _load_backtest_summary(self) -> Optional[Dict[str, Any]]:
        from src.services.backtest_service import BacktestService

        return BacktestService().get_summary(scope="overall", code=None)

    def _build_sample_gate_step(self, sample_count: int, min_samples: int, lookback_days: int) -> EvolutionStep:
        passed = sample_count >= min_samples
        reasons = [] if passed else ["insufficient_sample_size"]
        gate = EvolutionGateResult(
            gate_name="minimum_sample_size",
            passed=passed,
            sample_size=sample_count,
            reasons=reasons,
            metrics={"min_sample_size": min_samples, "lookback_days": lookback_days},
        )
        candidate = CandidateSuggestion(
            suggestion_id="sample-size-gate",
            suggestion_type="data_quality_gate",
            target="evolution.sample_window",
            rationale="样本不足时不应生成强结论，先扩大窗口或等待更多历史分析。",
            proposed_change={"lookback_days": max(lookback_days, 60), "min_sample_size": min_samples},
            rollback="恢复 EVOLUTION_LOOKBACK_DAYS 到当前值。",
            gate=gate,
        )
        return EvolutionStep(
            step_id="sample_gate",
            stage="evaluate",
            observed_issue="样本量检查",
            diagnosis="样本量达标后才允许后续建议进入人工评审。" if passed else "历史分析样本不足，当前只输出诊断轨迹。",
            candidates=[candidate],
            metrics={"sample_count": sample_count, "min_sample_size": min_samples},
            outcome_summary="passed" if passed else "blocked",
        )

    def _build_history_quality_steps(self, records: List[Any], min_samples: int) -> List[EvolutionStep]:
        if not records:
            return []

        missing_snapshot = 0
        missing_summary = 0
        static_theme_fallback = 0
        for record in records:
            if not getattr(record, "context_snapshot", None):
                missing_snapshot += 1
            if not getattr(record, "analysis_summary", None):
                missing_summary += 1
            snapshot = _parse_json_object(getattr(record, "context_snapshot", None))
            if _contains_value(snapshot, "theme_universe_source", "static_fallback"):
                static_theme_fallback += 1

        steps: List[EvolutionStep] = []
        snapshot_ratio = missing_snapshot / len(records)
        summary_ratio = missing_summary / len(records)
        if snapshot_ratio > 0.2 or summary_ratio > 0.2:
            passed = len(records) >= min_samples
            gate = EvolutionGateResult(
                gate_name="history_quality",
                passed=passed,
                sample_size=len(records),
                reasons=[] if passed else ["insufficient_sample_size"],
                metrics={
                    "missing_context_snapshot_ratio": round(snapshot_ratio, 4),
                    "missing_analysis_summary_ratio": round(summary_ratio, 4),
                },
            )
            steps.append(
                EvolutionStep(
                    step_id="history_quality",
                    stage="diagnose",
                    observed_issue="历史分析记录存在上下文或摘要缺口",
                    diagnosis="缺失上下文会削弱后续进化诊断可信度，应优先保证快照和摘要可用。",
                    candidates=[
                        CandidateSuggestion(
                            suggestion_id="preserve-context-snapshot",
                            suggestion_type="data_quality_gate",
                            target="SAVE_CONTEXT_SNAPSHOT",
                            rationale="自我进化依赖历史上下文快照定位数据源、LLM 与报告质量问题。",
                            proposed_change={"SAVE_CONTEXT_SNAPSHOT": "true"},
                            rollback="将 SAVE_CONTEXT_SNAPSHOT 恢复到变更前配置。",
                            gate=gate,
                        )
                    ],
                    metrics=gate.metrics,
                    outcome_summary="needs_review",
                )
            )

        if static_theme_fallback:
            gate = EvolutionGateResult(
                gate_name="realtime_hotspot_fact_gate",
                passed=False,
                sample_size=len(records),
                reasons=["static_theme_fallback_cannot_confirm_hotspot"],
                metrics={"static_theme_fallback_count": static_theme_fallback},
            )
            steps.append(
                EvolutionStep(
                    step_id="hotspot_fact_gate",
                    stage="diagnose",
                    observed_issue="热点扩散样本出现静态题材池降级",
                    diagnosis="盘中热点必须由实时行情事实确认，静态题材池只能作为补充标签或降级标记。",
                    candidates=[
                        CandidateSuggestion(
                            suggestion_id="require-realtime-hotspot-facts",
                            suggestion_type="screening_rule",
                            target="hot_theme_expansion.data_quality",
                            rationale="避免把静态题材池误当作热点入口。",
                            proposed_change={"require_realtime_snapshot_for_hotspot": True},
                            rollback="移除该候选建议，不改变现有筛选实现。",
                            gate=gate,
                        )
                    ],
                    metrics=gate.metrics,
                    outcome_summary="blocked",
                )
            )
        return steps

    def _build_backtest_steps(
        self,
        summary: Dict[str, Any],
        sample_count: int,
        min_samples: int,
    ) -> List[EvolutionStep]:
        if not summary:
            return []

        total = int(summary.get("total_evaluations") or 0)
        completed = int(summary.get("completed_count") or 0)
        insufficient = int(summary.get("insufficient_count") or 0)
        accuracy = _safe_float(summary.get("direction_accuracy_pct"))
        steps: List[EvolutionStep] = []

        if total and insufficient / total > 0.3:
            gate = EvolutionGateResult(
                gate_name="backtest_data_coverage",
                passed=sample_count >= min_samples,
                sample_size=sample_count,
                reasons=[] if sample_count >= min_samples else ["insufficient_sample_size"],
                metrics={"total_evaluations": total, "insufficient_count": insufficient},
            )
            steps.append(
                EvolutionStep(
                    step_id="backtest_data_coverage",
                    stage="diagnose",
                    observed_issue="回测结果中数据不足占比较高",
                    diagnosis="先补足历史行情或延后评估窗口，再判断分析质量。",
                    candidates=[
                        CandidateSuggestion(
                            suggestion_id="raise-backtest-min-age",
                            suggestion_type="config_threshold",
                            target="BACKTEST_MIN_AGE_DAYS",
                            rationale="减少最近数据不完整导致的 insufficient_data。",
                            proposed_change={"BACKTEST_MIN_AGE_DAYS": "increase_after_review"},
                            rollback="恢复 BACKTEST_MIN_AGE_DAYS 到当前值。",
                            gate=gate,
                        )
                    ],
                    metrics=gate.metrics,
                    outcome_summary="needs_review",
                )
            )

        if completed >= min_samples and accuracy is not None and accuracy < 50.0:
            gate = EvolutionGateResult(
                gate_name="direction_accuracy_floor",
                passed=True,
                sample_size=completed,
                metrics={"direction_accuracy_pct": accuracy, "completed_count": completed},
            )
            steps.append(
                EvolutionStep(
                    step_id="direction_accuracy",
                    stage="reflect",
                    observed_issue="历史方向判断准确率低于 50%",
                    diagnosis="应优先检查趋势判断 prompt 与数据质量约束，而不是直接扩大候选股票范围。",
                    candidates=[
                        CandidateSuggestion(
                            suggestion_id="tighten-direction-evidence",
                            suggestion_type="prompt_fragment",
                            target="analysis.prompt.direction_evidence",
                            rationale="方向结论需要引用实时行情、均线、量能和回测反馈，降低空泛判断。",
                            proposed_change={"require_direction_evidence": ["price_action", "volume", "moving_average", "data_quality"]},
                            rollback="移除该 prompt 片段建议。",
                            gate=gate,
                        )
                    ],
                    metrics=gate.metrics,
                    outcome_summary="candidate_ready_for_review",
                )
            )
        return steps


def _parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _contains_value(payload: Any, key: str, expected: Any) -> bool:
    if isinstance(payload, dict):
        for current_key, current_value in payload.items():
            if current_key == key and current_value == expected:
                return True
            if _contains_value(current_value, key, expected):
                return True
    elif isinstance(payload, list):
        return any(_contains_value(item, key, expected) for item in payload)
    return False


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
