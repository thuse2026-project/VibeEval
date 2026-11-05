"""Small OpenAI-compatible HTTP client. No automatic retries or tool execution."""

import asyncio
from typing import Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .config import LLMError, LLMSettings


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16000)

    @field_validator("content")
    @classmethod
    def check_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息内容不能为空")
        # Preserve indentation and newlines when messages contain code.
        return value


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    messages: list[Message] = Field(min_length=1, max_length=40)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int | None = Field(default=None, ge=1, le=4096)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def check_context_size(self):
        if sum(len(message.content) for message in self.messages) > 64000:
            raise ValueError("消息总长度不能超过 64000 字符")
        return self


class Usage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ChatResult(BaseModel):
    id: str | None = None
    model: str
    content: str
    finish_reason: str | None = None
    usage: Usage | None = None


class ModelInfo(BaseModel):
    id: str = Field(min_length=1)
    name: str | None = None
    context_length: int | None = Field(default=None, ge=1)


def upstream_error(status: int) -> LLMError:
    messages = {
        401: (502, "模型服务拒绝 API Key，请检查本地配置"),
        403: (502, "模型服务拒绝访问，请检查 Key 权限或模型限制"),
        402: (503, "模型服务额度不足，请检查余额或切换可用模型"),
        404: (502, "模型或 API 地址不可用，请检查配置"),
        429: (429, "模型服务限流，请稍后重试或切换模型"),
    }
    return LLMError(*messages.get(status, (502, "模型服务请求失败，请稍后重试")))


class LLMClient:
    def __init__(self, settings: LLMSettings, *, transport=None):
        self.settings = settings
        self.transport = transport

    async def _request(self, method: str, path: str, body=None) -> dict:
        if not self.settings.api_key:
            raise LLMError(503, "尚未配置 LLM_API_KEY，请在后端本地配置中设置")
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.base_url + "/",
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                timeout=httpx.Timeout(self.settings.timeout_seconds, connect=10),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await asyncio.wait_for(
                    client.request(method, path, json=body),
                    timeout=self.settings.timeout_seconds,
                )
        except (httpx.TimeoutException, TimeoutError):
            raise LLMError(504, "模型服务响应超时，请稍后重试") from None
        except httpx.RequestError:
            raise LLMError(502, "暂时无法连接模型服务，请检查网络和 API 地址") from None
        if not response.is_success:
            # Never expose provider bodies, URLs, headers or credentials to callers.
            raise upstream_error(response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise LLMError(502, "模型服务返回了无效数据") from None
        if not isinstance(payload, dict):
            raise LLMError(502, "模型服务返回了无效数据")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise upstream_error(code if isinstance(code, int) else 502)
        return payload

    async def models(self) -> list[ModelInfo]:
        payload = await self._request("GET", "models")
        try:
            if not isinstance(payload["data"], list):
                raise TypeError
            return [ModelInfo.model_validate(item) for item in payload["data"]]
        except (KeyError, TypeError, ValidationError):
            raise LLMError(502, "模型服务返回的模型列表无效") from None

    async def chat(self, body: ChatInput) -> ChatResult:
        model = body.model or self.settings.model
        if not model:
            raise LLMError(
                503, "尚未配置 LLM_MODEL，请配置默认模型或在请求中指定 model"
            )
        if body.max_tokens and body.max_tokens > self.settings.max_output_tokens:
            raise LLMError(422, "max_tokens 超过后端配置的输出上限")
        request = {
            "model": model,
            "messages": [message.model_dump() for message in body.messages],
            "max_tokens": body.max_tokens or self.settings.max_output_tokens,
            "stream": False,
        }
        if body.temperature is not None:
            request["temperature"] = body.temperature
        payload = await self._request("POST", "chat/completions", request)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return ChatResult(
                id=payload.get("id"),
                model=payload.get("model") or model,
                content=content,
                finish_reason=choice.get("finish_reason"),
                usage=payload.get("usage"),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise LLMError(502, "模型未返回有效文本，请调整模型或输出上限") from None
