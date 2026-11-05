"""Load private model settings from the repository .env or process environment."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    timeout_seconds: float = 45
    max_output_tokens: int = 1024


def load_settings() -> LLMSettings:
    values = {**dotenv_values(ENV_FILE, interpolate=False), **os.environ}
    base_url = (values.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1").strip()
    base_url = base_url.rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (
            url.scheme in ("http", "https")
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
            and not any(char.isspace() for char in base_url)
            and (url.port is None or 1 <= url.port <= 65535)
            and not url.path.endswith(("/chat/completions", "/models"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise LLMError(503, "LLM_BASE_URL 配置无效，请填写 API 基础地址")
    key = (values.get("LLM_API_KEY") or "").strip()
    if any(char.isspace() for char in key) or not key.isascii():
        raise LLMError(503, "LLM_API_KEY 配置无效")
    try:
        timeout = float(values.get("LLM_TIMEOUT_SECONDS", "45"))
        limit = int(values.get("LLM_MAX_OUTPUT_TOKENS", "1024"))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError
        if not 1 <= limit <= 4096:
            raise ValueError
    except (TypeError, ValueError):
        raise LLMError(503, "模型超时或输出上限配置无效") from None
    return LLMSettings(
        base_url=base_url,
        api_key=key,
        model=(values.get("LLM_MODEL") or "").strip(),
        timeout_seconds=timeout,
        max_output_tokens=limit,
    )
