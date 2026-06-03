# -*- coding: utf-8 -*-
"""Tests for the restricted Claude bridge context service."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.claude_bridge_service import ClaudeBridgeService


class ClaudeBridgeServiceTests(unittest.TestCase):
    def test_build_stock_user_message_includes_guardrails_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            (project_root / "CLAUDE.md").write_text("Always answer in Chinese", encoding="utf-8")

            memory_root = tmp_path / "memory"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("- project preference", encoding="utf-8")
            (memory_root / "project_context.md").write_text("Use Feishu", encoding="utf-8")

            service = ClaudeBridgeService(project_root=project_root, memory_root=memory_root)

            message = service.build_stock_user_message(
                "603687",
                skill_id="bull_trend",
                skill_text="看风险",
                metrics_context={"code": "603687", "twenty_day": {"twenty_day_return_pct": 12.3}},
            )

        self.assertIn("daily_stock_analysis", message)
        self.assertIn("603687", message)
        self.assertIn("禁止下单", message)
        self.assertIn("Always answer in Chinese", message)
        self.assertIn("project preference", message)
        self.assertIn("Use Feishu", message)
        self.assertIn("确定性问股指标", message)
        self.assertIn("twenty_day_return_pct", message)
        self.assertIn("基本面、分时、趋势、20日涨幅、大盘、热点", message)

    def test_restricted_context_skips_sensitive_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            (project_root / "CLAUDE.md").write_text("safe", encoding="utf-8")

            memory_root = tmp_path / "memory"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("index", encoding="utf-8")
            (memory_root / "project_safe.md").write_text("safe memory", encoding="utf-8")
            (memory_root / "project_secret.md").write_text("SECRET_SHOULD_NOT_APPEAR", encoding="utf-8")

            log_path = project_root / "logs" / "debug.log"
            log_path.parent.mkdir()
            log_path.write_text("log secret", encoding="utf-8")

            service = ClaudeBridgeService(project_root=project_root, memory_root=memory_root)
            context = service.build_restricted_context()

            self.assertIn("safe", context)
            self.assertIn("safe memory", context)
            self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", context)
            self.assertEqual(service._read_safe_text(log_path), "")
            self.assertEqual(service._read_safe_text(project_root / ".env"), "")

    def test_build_stock_answer_uses_anthropic_direct_client(self):
        service = ClaudeBridgeService(project_root=Path(__file__).parent, memory_root=Path(__file__).parent)
        client = MagicMock()
        client.create_message.return_value = SimpleNamespace(content="direct answer")

        with patch("src.services.claude_bridge_service.AnthropicDirectClient", return_value=client):
            answer = service.build_stock_answer(
                "600519",
                skill_id="bull_trend",
                skill_text="看风险",
                config=SimpleNamespace(),
                metrics_context={"market": {"bias": "risk_on"}},
            )

        self.assertEqual(answer, "direct answer")
        client.create_message.assert_called_once()
        user_message = client.create_message.call_args.args[0]
        self.assertIn("600519", user_message)
        self.assertIn("bull_trend", user_message)
        self.assertIn("看风险", user_message)
        self.assertIn("risk_on", user_message)
        system_prompt = client.create_message.call_args.kwargs["system"]
        self.assertIn("基本面", system_prompt)
        self.assertIn("情景推演", system_prompt)


if __name__ == "__main__":
    unittest.main()
