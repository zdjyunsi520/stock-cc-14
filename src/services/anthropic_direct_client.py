# -*- coding: utf-8 -*-
"""Direct Anthropic-format Messages API client."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from src.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class AnthropicDirectResponse:
    content: str
    model: str
    usage: Dict[str, Any]


class AnthropicDirectClient:
    """Call Anthropic-format Messages API without LiteLLM."""

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    DEFAULT_VERSION = "2023-06-01"

    def __init__(self, config=None, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        self.config = config or get_config()
        channel_api_key, channel_base_url, channel_model = self._resolve_anthropic_channel()
        config_keys = getattr(self.config, "anthropic_api_keys", []) or []
        config_api_key = getattr(self.config, "anthropic_api_key", None) or (config_keys[0] if config_keys else "")
        self.api_key = (api_key or os.getenv("CLAUDE_BRIDGE_API_KEY") or config_api_key or channel_api_key or "").strip()
        self.base_url = (base_url or os.getenv("CLAUDE_BRIDGE_BASE_URL") or channel_base_url or os.getenv("ANTHROPIC_BASE_URL") or self.DEFAULT_BASE_URL).strip()
        self.model = (model or os.getenv("CLAUDE_BRIDGE_MODEL") or channel_model or getattr(self.config, "anthropic_model", "") or "claude-opus-4-6").strip()
        self.messages_path = os.getenv("CLAUDE_BRIDGE_MESSAGES_PATH", "").strip()
        self.timeout_s = float(os.getenv("CLAUDE_BRIDGE_TIMEOUT_S", "240"))

    def _resolve_anthropic_channel(self) -> Tuple[str, str, str]:
        for channel in self._iter_anthropic_channels():
            api_keys = channel.get("api_keys") if isinstance(channel.get("api_keys"), list) else []
            api_key = str(channel.get("api_key") or (api_keys[0] if api_keys else "")).strip()
            base_url = str(channel.get("base_url") or "").strip()
            models = channel.get("models") if isinstance(channel.get("models"), list) else []
            model = str(models[0] if models else channel.get("model") or "").strip()
            if api_key or base_url or model:
                return api_key, base_url, self._strip_provider_prefix(model)
        return "", "", ""

    def _iter_anthropic_channels(self) -> List[Dict[str, Any]]:
        channels = []
        for channel in getattr(self.config, "llm_channels", []) or []:
            if isinstance(channel, dict) and self._is_anthropic_channel(channel):
                channels.append(channel)
        channels.extend(self._parse_env_channels())
        return channels

    @staticmethod
    def _is_anthropic_channel(channel: Dict[str, Any]) -> bool:
        protocol = str(channel.get("protocol") or "").strip().lower().replace("-", "_")
        name = str(channel.get("name") or "").strip().lower()
        models = channel.get("models") if isinstance(channel.get("models"), list) else []
        first_model = str(models[0] if models else channel.get("model") or "").strip().lower()
        return protocol in {"anthropic", "claude"} or name in {"anthropic", "claude"} or first_model.startswith("anthropic/")

    @staticmethod
    def _parse_env_channels() -> List[Dict[str, Any]]:
        channels = []
        for raw_name in os.getenv("LLM_CHANNELS", "").split(","):
            name = raw_name.strip()
            if not name:
                continue
            prefix = f"LLM_{name.upper().replace('-', '_')}_"
            api_keys_raw = os.getenv(prefix + "API_KEYS") or os.getenv(prefix + "API_KEY") or ""
            models_raw = os.getenv(prefix + "MODELS") or os.getenv(prefix + "MODEL") or ""
            channel = {
                "name": name,
                "protocol": os.getenv(prefix + "PROTOCOL", ""),
                "base_url": os.getenv(prefix + "BASE_URL", ""),
                "api_keys": [item.strip() for item in api_keys_raw.split(",") if item.strip()],
                "models": [item.strip() for item in models_raw.split(",") if item.strip()],
            }
            if AnthropicDirectClient._is_anthropic_channel(channel):
                channels.append(channel)
        return channels

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        if model.lower().startswith("anthropic/"):
            return model.split("/", 1)[1]
        return model

    @property
    def is_available(self) -> bool:
        return bool(self.api_key and self.model)

    def messages_url(self) -> str:
        base = self.base_url.rstrip("/")
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("ANTHROPIC_BASE_URL 必须是完整 http(s) URL")
        if self.messages_path:
            path = self.messages_path if self.messages_path.startswith("/") else f"/{self.messages_path}"
            return f"{base}{path}"
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def create_message(
        self,
        user_message: str,
        *,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> AnthropicDirectResponse:
        if not self.is_available:
            raise RuntimeError(
                "Anthropic direct client 未配置，请设置 CLAUDE_BRIDGE_*、ANTHROPIC_*，"
                "或 LLM_CHANNELS 中 protocol=anthropic 的中转站配置"
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens or min(getattr(self.config, "anthropic_max_tokens", 8192), 4096)),
            "temperature": float(temperature if temperature is not None else getattr(self.config, "anthropic_temperature", 0.7)),
            "messages": [{"role": "user", "content": user_message}],
        }
        if system.strip():
            payload["system"] = system.strip()

        # 超时重试：最多 3 次，每次递增超时
        max_retries = 3
        last_exc = None
        for retry in range(max_retries):
            try:
                timeout = self.timeout_s * (retry + 1)
                response = requests.post(
                    self.messages_url(),
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": self.DEFAULT_VERSION,
                        "content-type": "application/json",
                    },
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=timeout,
                )
                break
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                logger.warning(
                    "Anthropic direct call timeout (retry %d/%d, timeout=%.0fs): %s",
                    retry + 1, max_retries, timeout, exc,
                )
                if retry < max_retries - 1:
                    import time
                    time.sleep(2)
        else:
            raise RuntimeError(
                f"Anthropic direct call timed out after {max_retries} retries: "
                f"model={self.model}; url={self.messages_url()}"
            ) from last_exc
        if response.status_code >= 400:
            body_summary = self._summarize_error_body(response.text)
            logger.warning(
                "Anthropic direct call failed: status=%s model=%s url=%s body=%s",
                response.status_code,
                self.model,
                self.messages_url(),
                body_summary,
            )
            raise RuntimeError(
                f"Anthropic direct call failed: HTTP {response.status_code}; "
                f"model={self.model}; url={self.messages_url()}; body={body_summary}"
            )

        data = response.json()
        return AnthropicDirectResponse(
            content=self._extract_text(data),
            model=str(data.get("model") or self.model),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        )

    @staticmethod
    def _summarize_error_body(text: str) -> str:
        cleaned = " ".join(str(text or "").split())[:500]
        for marker in ("sk-", "Bearer "):
            if marker in cleaned:
                cleaned = cleaned.split(marker, 1)[0] + marker + "***"
        return cleaned or "<empty>"

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        parts: List[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip())
