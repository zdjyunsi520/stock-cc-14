# -*- coding: utf-8 -*-
"""Tests for direct Anthropic-format client."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.anthropic_direct_client import AnthropicDirectClient


class AnthropicDirectClientTests(unittest.TestCase):
    def test_messages_url_accepts_base_host_or_v1(self):
        config = SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-opus-4-6")

        self.assertEqual(
            AnthropicDirectClient(config=config, base_url="https://proxy.example.com").messages_url(),
            "https://proxy.example.com/v1/messages",
        )
        self.assertEqual(
            AnthropicDirectClient(config=config, base_url="https://proxy.example.com/v1").messages_url(),
            "https://proxy.example.com/v1/messages",
        )
        self.assertEqual(
            AnthropicDirectClient(config=config, base_url="https://proxy.example.com/v1/messages").messages_url(),
            "https://proxy.example.com/v1/messages",
        )

    def test_create_message_posts_anthropic_payload_without_litellm(self):
        config = SimpleNamespace(
            anthropic_api_key="sk-test",
            anthropic_model="claude-opus-4-6",
            anthropic_max_tokens=2048,
            anthropic_temperature=0.3,
            llm_channels=[],
        )
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "分析结果"}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

        env_clear = {
            "CLAUDE_BRIDGE_API_KEY": "",
            "CLAUDE_BRIDGE_BASE_URL": "",
            "CLAUDE_BRIDGE_MODEL": "",
        }
        with patch.dict("os.environ", env_clear, clear=False):
            with patch("src.services.anthropic_direct_client.requests.post", return_value=fake_response) as post:
                response = AnthropicDirectClient(
                    config=config,
                    base_url="https://proxy.example.com/v1",
                ).create_message("问股", system="系统边界")

        self.assertEqual(response.content, "分析结果")
        url, = post.call_args.args
        kwargs = post.call_args.kwargs
        self.assertEqual(url, "https://proxy.example.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "sk-test")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertIn('"system": "系统边界"', kwargs["data"].decode("utf-8"))
        self.assertIn('"messages": [{"role": "user", "content": "问股"}]', kwargs["data"].decode("utf-8"))

    def test_uses_anthropic_llm_channel_when_anthropic_env_is_absent(self):
        config = SimpleNamespace(
            anthropic_api_key="",
            anthropic_api_keys=[],
            anthropic_model="claude-opus-4-6",
            llm_channels=[{
                "name": "proxy",
                "protocol": "anthropic",
                "base_url": "https://proxy.example.com",
                "api_keys": ["sk-channel"],
                "models": ["anthropic/gpt-5.5"],
            }],
        )

        client = AnthropicDirectClient(config=config)

        self.assertTrue(client.is_available)
        self.assertEqual(client.api_key, "sk-channel")
        self.assertEqual(client.base_url, "https://proxy.example.com")
        self.assertEqual(client.model, "gpt-5.5")
        self.assertEqual(client.messages_url(), "https://proxy.example.com/v1/messages")

    def test_uses_llm_channels_environment_when_config_has_no_channels(self):
        config = SimpleNamespace(
            anthropic_api_key="",
            anthropic_api_keys=[],
            anthropic_model="claude-opus-4-6",
            llm_channels=[],
        )
        env = {
            "LLM_CHANNELS": "claude_proxy",
            "LLM_CLAUDE_PROXY_PROTOCOL": "anthropic",
            "LLM_CLAUDE_PROXY_BASE_URL": "https://proxy.example.com/v1",
            "LLM_CLAUDE_PROXY_API_KEY": "sk-env-channel",
            "LLM_CLAUDE_PROXY_MODELS": "anthropic/gpt-5.5",
        }

        with patch.dict("os.environ", env, clear=False):
            client = AnthropicDirectClient(config=config)

        self.assertEqual(client.api_key, "sk-env-channel")
        self.assertEqual(client.base_url, "https://proxy.example.com/v1")
        self.assertEqual(client.model, "gpt-5.5")

    def test_messages_path_override_supports_nonstandard_proxy_paths(self):
        config = SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-opus-4-6", llm_channels=[])

        with patch.dict("os.environ", {"CLAUDE_BRIDGE_MESSAGES_PATH": "/messages"}, clear=False):
            client = AnthropicDirectClient(config=config, base_url="https://proxy.example.com/claude/aws")

        self.assertEqual(client.messages_url(), "https://proxy.example.com/claude/aws/messages")

    def test_http_error_includes_sanitized_body_model_and_url(self):
        config = SimpleNamespace(
            anthropic_api_key="sk-test",
            anthropic_model="gpt-5.5",
            anthropic_max_tokens=2048,
            anthropic_temperature=0.3,
            llm_channels=[],
        )
        fake_response = MagicMock()
        fake_response.status_code = 503
        fake_response.text = '{"error":"upstream unavailable for sk-secret-value"}'

        with patch("src.services.anthropic_direct_client.requests.post", return_value=fake_response):
            with self.assertRaises(RuntimeError) as cm:
                AnthropicDirectClient(config=config, base_url="https://proxy.example.com").create_message("问股")

        message = str(cm.exception)
        self.assertIn("HTTP 503", message)
        self.assertIn("model=gpt-5.5", message)
        self.assertIn("url=https://proxy.example.com/v1/messages", message)
        self.assertIn("body=", message)
        self.assertIn("sk-***", message)
        self.assertNotIn("sk-secret-value", message)


if __name__ == "__main__":
    unittest.main()
