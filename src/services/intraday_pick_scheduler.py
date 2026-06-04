# -*- coding: utf-8 -*-
"""Scheduled intraday stock-picking workflow with Claude-hosted push reports."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.notification import NotificationBuilder, NotificationDispatchResult, NotificationService
from src.services.claude_market_host_agent import ClaudeMarketHostAgent
from src.services.hot_theme_expansion_screener import (
    HotThemeExpansionCandidate,
    HotThemeExpansionCriteria,
    screen_hot_theme_expansion,
)

logger = logging.getLogger(__name__)


class IntradayPickScheduler:
    """Run the intraday picker at configured time slots and push one report."""

    _REJECTED_REASON_LABELS = {
        "daily_history_unavailable": "日线历史数据不可用",
        "ma5_ma10_ma20_not_all_upward": "5/10/20 日均线未全部向上",
        "no_limit_up_in_20_days": "近 20 日无涨停记忆",
        "capital_flow_abnormal": "资金异动或资金条件不符合",
        "intraday_pattern_not_confirmed": "分时形态未确认：未满足全天站上均线、创新高回踩不破或尾盘稳步拉升等条件",
    }

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        notifier: Optional[NotificationService] = None,
        host_agent_factory: Optional[Callable[[Any], ClaudeMarketHostAgent]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        clock_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self.notifier = notifier
        self.host_agent_factory = host_agent_factory or (lambda config: ClaudeMarketHostAgent(config=config))
        self.now_provider = now_provider or datetime.now
        self.clock_provider = clock_provider or time.time
        self._slot_runs: Dict[str, float] = {}
        self._slot_retry_after: Dict[str, float] = {}

    @staticmethod
    def _default_config_provider() -> Any:
        from src.config import get_config

        return get_config()

    def run_once(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "checked": 0,
            "rough": 0,
            "passed": 0,
            "ranked": 0,
            "notified": 0,
            "skipped": 0,
            "used_claude": 0,
            "archived": 0,
            "archive_path": "",
            "slot": "",
            "retry_after_seconds": 0,
        }
        config = self.config_provider()
        if not getattr(config, "intraday_pick_enabled", False):
            stats["skipped"] = 1
            return stats

        now = self.now_provider()
        slot = self._match_due_slot(config, now)
        if slot is None:
            stats["skipped"] = 1
            return stats
        stats["slot"] = slot

        if self._should_skip_for_trading_session(config):
            stats["skipped"] = 1
            return stats

        run_result = self._build_report_run(config, slot, now)
        data_source_failed = bool(run_result.pop("data_source_failed", False))
        content = str(run_result.pop("content", ""))
        stats.update(run_result)
        if data_source_failed:
            self._schedule_slot_retry(slot)
            stats["retry_after_seconds"] = 600
        if getattr(config, "intraday_pick_dry_run", True):
            logger.info("[IntradayPick] dry-run 报告:\n%s", content)
            if not data_source_failed:
                self._mark_slot_run(slot)
            return stats

        dispatch = self._send_notification(config, slot, content)
        if getattr(dispatch, "success", False):
            stats["notified"] = 1
            if not data_source_failed:
                self._mark_slot_run(slot)
        return stats

    def run_manual(self, slot: Optional[str] = None) -> Dict[str, Any]:
        """Run one manual intraday pick report without slot/time gating."""
        config = self.config_provider()
        now = self.now_provider()
        report_slot = slot or now.strftime("%H:%M")
        run_result = self._build_report_run(config, report_slot, now)
        content = str(run_result.get("content") or "")
        return {**run_result, "slot": report_slot, "content": content}

    def _build_report_run(self, config: Any, slot: str, now: datetime) -> Dict[str, Any]:
        criteria = self._build_criteria(config)
        result = screen_hot_theme_expansion(criteria)
        data_source_failed = (result.data_quality or {}).get("snapshot_status") == "source_failed"
        ranked = self._rank_candidates(result.candidates)
        priority_count = sum(1 for item in ranked if item.laggard_priority > 0)
        display_limit = int(getattr(config, "intraday_pick_report_limit", 10) or 10)
        archive_payload = self._build_payload(config, slot, now, result, ranked)
        archive_path = self._archive_payload(config, archive_payload)
        payload = {**archive_payload, "ranked_candidates": archive_payload["ranked_candidates"][:display_limit]}
        fallback_content = self._build_fallback_report(payload)
        host_report = self.host_agent_factory(config).build_report(payload, fallback_content)
        content = self._wrap_report(host_report.content, host_report.used_claude, host_report.model)
        return {
            "checked": len(result.candidates),
            "rough": int(result.data_quality.get("matched_snapshot_count") or 0),
            "passed": priority_count,
            "ranked": len(ranked),
            "used_claude": 1 if host_report.used_claude else 0,
            "archived": 1 if archive_path else 0,
            "archive_path": archive_path,
            "content": content,
            "data_source_failed": data_source_failed,
        }

    def _match_due_slot(self, config: Any, now: datetime) -> Optional[str]:
        window_minutes = max(0, int(getattr(config, "intraday_pick_time_window_minutes", 3) or 3))
        now_minutes = now.hour * 60 + now.minute
        current_clock = self.clock_provider()
        for slot in self._resolve_slots(config):
            run_key = f"{now.date().isoformat()} {slot}"
            if run_key in self._slot_runs:
                continue
            retry_after = self._slot_retry_after.get(run_key)
            if retry_after is not None:
                if current_clock >= retry_after:
                    self._slot_retry_after.pop(run_key, None)
                    return slot
                continue
            try:
                hour_text, minute_text = slot.split(":", 1)
                slot_minutes = int(hour_text) * 60 + int(minute_text)
            except (TypeError, ValueError):
                continue
            if now_minutes < slot_minutes or now_minutes - slot_minutes > window_minutes:
                continue
            return slot
        return None

    @staticmethod
    def _resolve_slots(config: Any) -> List[str]:
        raw_slots = getattr(config, "intraday_pick_push_times", None) or ["10:20", "13:45", "14:35"]
        slots: List[str] = []
        for raw in raw_slots:
            text = str(raw or "").strip()
            if text and text not in slots:
                slots.append(text)
        return slots

    def _should_skip_for_trading_session(self, config: Any) -> bool:
        if not getattr(config, "intraday_pick_trading_hours_only", True):
            return False
        try:
            from src.core.trading_calendar import build_market_phase_context

            context = build_market_phase_context(
                market="cn",
                trigger_source="intraday_pick",
                analysis_intent="intraday_pick",
            )
            return context.is_market_open_now is False
        except Exception as exc:
            logger.warning("[IntradayPick] 交易时段判断失败，继续执行: %s", exc)
            return False

    @staticmethod
    def _build_criteria(config: Any) -> HotThemeExpansionCriteria:
        return HotThemeExpansionCriteria(
            max_candidates=int(getattr(config, "intraday_pick_max_candidates_for_deep_check", 80) or 80),
        )

    @staticmethod
    def _rank_candidates(candidates: Sequence[HotThemeExpansionCandidate]) -> List[HotThemeExpansionCandidate]:
        return sorted(
            candidates,
            key=lambda item: (
                item.laggard_priority,
                item.score,
                item.theme_score,
                item.change_pct or -99.0,
            ),
            reverse=True,
        )

    @staticmethod
    def _build_payload(
        config: Any,
        slot: str,
        now: datetime,
        result: Any,
        ranked: Sequence[HotThemeExpansionCandidate],
    ) -> Dict[str, Any]:
        data_quality = result.data_quality or {}
        priority_count = sum(1 for item in ranked if item.laggard_priority > 0)
        return {
            "title": f"热点扩散盘中选股 {slot}",
            "slot": slot,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "rough_count": data_quality.get("matched_snapshot_count", 0),
                "checked_count": len(ranked),
                "passed_count": priority_count,
                "rejected_count": max(0, len(ranked) - priority_count),
                "theme_count": len(result.themes),
                "active_theme_count": data_quality.get("active_theme_count", 0),
                "data_quality": data_quality,
            },
            "themes": [asdict(item) for item in result.themes],
            "criteria": asdict(result.criteria),
            "ranked_candidates": [IntradayPickScheduler._candidate_payload(item) for item in ranked],
            "notes": [
                "热点扩散规则引擎负责筛选和排序，Claude 只负责解释与风险提示。",
                "本报告是只读观察提醒，不代表自动交易指令。",
            ],
            "dry_run": bool(getattr(config, "intraday_pick_dry_run", True)),
        }

    @staticmethod
    def _archive_payload(config: Any, payload: Dict[str, Any]) -> str:
        generated_at = str(payload.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        slot = str(payload.get("slot") or "unknown").replace(":", "")
        filename_time = "".join(ch for ch in generated_at if ch.isdigit())[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
        log_dir = Path(getattr(config, "log_dir", "./logs") or "./logs")
        archive_dir = log_dir / "intraday_pick_archive"
        archive_path = archive_dir / f"intraday_pick_{filename_time}_{slot}.json"
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            return str(archive_path)
        except Exception as exc:
            logger.warning("[IntradayPick] 归档筛选结果失败: %s", exc)
            return ""

    @staticmethod
    def _candidate_payload(candidate: HotThemeExpansionCandidate) -> Dict[str, Any]:
        return {
            "code": candidate.code,
            "name": candidate.name,
            "themes": candidate.themes,
            "price": candidate.price,
            "change_pct": candidate.change_pct,
            "volume_ratio": candidate.volume_ratio,
            "turnover_rate": candidate.turnover_rate,
            "circ_mv_yi": round(candidate.circ_mv / 100000000, 2) if candidate.circ_mv is not None else None,
            "score": candidate.score,
            "theme_score": candidate.theme_score,
            "laggard_priority": candidate.laggard_priority,
            "passed": candidate.laggard_priority > 0,
            "rejected_reasons": candidate.warnings,
            "reliability_score": candidate.score,
            "reliability_reasons": candidate.reasons,
            "warnings": candidate.warnings,
            "metrics": candidate.metrics,
            "data_quality": candidate.data_quality,
        }

    @classmethod
    def _format_rejected_reasons(cls, reasons: Sequence[str]) -> str:
        labels = [cls._REJECTED_REASON_LABELS.get(str(reason), str(reason)) for reason in reasons]
        return "；".join(label for label in labels if label)

    @classmethod
    def _build_fallback_report(cls, payload: Dict[str, Any]) -> str:
        summary = payload.get("summary") or {}
        candidates = payload.get("ranked_candidates") or []
        data_quality = summary.get("data_quality") or {}
        source_failed = data_quality.get("snapshot_status") == "source_failed"
        static_degraded = data_quality.get("theme_universe_degraded") or data_quality.get("theme_universe_source") == "static_fallback"
        snapshot_empty = source_failed or data_quality.get("snapshot_status") == "empty" or data_quality.get("snapshot_count") == 0
        conclusion = (
            "核心结论：数据源获取失败，本次盘中选股不可用，系统将在约 10 分钟后再试一次。"
            if source_failed
            else "核心结论：实时行情快照为空，本次盘中选股不可用，不应据此判断没有机会。"
            if snapshot_empty
            else f"核心结论：实时热点源不可用，当前为静态题材兜底观察，匹配 {summary.get('rough_count', 0)} 只，观察候选 {summary.get('checked_count', 0)} 只。"
            if static_degraded
            else f"核心结论：匹配热点池 {summary.get('rough_count', 0)} 只，观察候选 {summary.get('checked_count', 0)} 只，低位补涨优先 {summary.get('passed_count', 0)} 只。"
        )
        lines = [
            f"**{payload.get('title', '热点扩散盘中选股')}**",
            "",
            conclusion,
            f"活跃题材：{summary.get('active_theme_count', 0)}/{summary.get('theme_count', 0)}。",
            "",
            "**题材状态**",
        ]
        themes = payload.get("themes") or []
        if source_failed:
            lines.append("腾讯批量、efinance/东财等实时数据源均未返回可用数据，题材状态无法计算。")
        elif snapshot_empty:
            lines.append("实时行情接口未返回可用快照，题材状态无法计算。")
        elif not themes:
            lines.append("暂无活跃题材。")
        for item in themes[:5]:
            reasons = "；".join((item.get("reasons") or [])[:3]) or "规则评分靠前"
            momentum_stage = str(item.get("momentum_stage") or "unknown")
            momentum_text = f"/{momentum_stage}" if momentum_stage != "unknown" else ""
            lifecycle_stage = str(item.get("lifecycle_stage") or "unknown")
            lifecycle_text = cls._lifecycle_stage_label(lifecycle_stage)
            lines.append(
                f"- {item.get('theme')}：{item.get('stage')}{momentum_text}，生命周期 {lifecycle_text}，"
                f"题材分 {item.get('score')}，基础分 {item.get('base_score', item.get('score'))}，"
                f"曲率 {item.get('momentum_score', 0)}，高潮压力 {item.get('climax_pressure', 0)}，"
                f"分歧 {item.get('divergence_score', 0)}，上涨 {item.get('up_count')}/{item.get('member_count')}，"
                f"活跃 {item.get('active_count')}。{reasons}"
            )
        lines.extend(["", "**候选排序**"])
        if source_failed:
            lines.append("数据源获取失败，候选列表无效；等待 10 分钟后重试。")
        elif snapshot_empty:
            lines.append("实时行情快照为空，候选列表无效。")
        elif not candidates:
            lines.append("暂无符合条件候选。")
        for idx, item in enumerate(candidates, 1):
            reasons = "；".join((item.get("reliability_reasons") or [])[:3]) or "热点扩散评分靠前"
            themes_text = "/".join(item.get("themes") or []) or "未标注题材"
            status = "低位补涨优先" if item.get("passed") else "观察/等待确认"
            lines.append(
                f"{idx}. {item.get('code')} {item.get('name')}（{themes_text}）：评分 {item.get('reliability_score')}，"
                f"题材分 {item.get('theme_score')}，涨幅 {item.get('change_pct')}%，量比 {item.get('volume_ratio')}，换手 {item.get('turnover_rate')}%，{status}。"
            )
            lines.append(f"   理由：{reasons}")
            if item.get("rejected_reasons"):
                risk_text = cls._format_rejected_reasons(item.get("rejected_reasons") or [])
                lines.append(f"   风险：{risk_text}")
        lines.extend(
            [
                "",
                "**下一步观察**",
                "- 热点题材是否继续扩散，而不是只剩单一核心票上涨。",
                "- 低位补涨票是否保持分时均线承接，尾盘是否放量回落。",
                "- 趋势确认票是否过热，等待回踩后再观察。",
                "",
                "**数据说明**",
                f"本报告由规则引擎生成，Claude 主持人未参与或已降级。数据状态：{data_quality.get('snapshot_status', 'unknown')}；热点来源：{data_quality.get('theme_universe_source', 'unknown')}；历史题材样本：{data_quality.get('theme_history_theme_count', 0)}。仅作观察提醒，不代表交易指令。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _lifecycle_stage_label(stage: str) -> str:
        labels = {
            "warming": "蠢蠢欲动",
            "accelerating": "升温扩散",
            "climax": "如日中天",
            "diverging": "分歧",
            "cooling": "退热",
            "exhausted": "退潮",
            "static_degraded": "静态兜底",
            "unknown": "未知",
        }
        return labels.get(stage, stage or "未知")

    @staticmethod
    def _wrap_report(content: str, used_claude: bool, model: str) -> str:
        suffix = "Claude 主持" if used_claude else "规则降级"
        if used_claude and model:
            suffix += f"（{model}）"
        return f"{content.strip()}\n\n---\n{suffix}；只读观察提醒，不自动交易。"

    def _send_notification(self, config: Any, slot: str, content: str) -> NotificationDispatchResult:
        notification_service = self.notifier or NotificationService()
        if not notification_service.is_available():
            return NotificationDispatchResult(dispatched=False, success=False, status="no_channel")
        title = f"热点扩散盘中选股 {slot}"
        alert_text = NotificationBuilder.build_simple_alert(title=title, content=content, alert_type="info")
        key = f"intraday_pick:{self.now_provider().date().isoformat()}:{slot}"
        return notification_service.send_with_results(
            alert_text,
            route_type="alert",
            severity="info",
            dedup_key=key,
            cooldown_key=key,
        )

    def _schedule_slot_retry(self, slot: str) -> None:
        today = self.now_provider().date().isoformat()
        run_key = f"{today} {slot}"
        retry_at = self.clock_provider() + 600
        self._slot_retry_after[run_key] = retry_at
        logger.warning("[IntradayPick] 数据源获取失败，slot=%s 将在 10 分钟后重试", slot)

    def _mark_slot_run(self, slot: str) -> None:
        today = self.now_provider().date().isoformat()
        run_key = f"{today} {slot}"
        self._slot_runs[run_key] = self.clock_provider()
        self._slot_retry_after.pop(run_key, None)
