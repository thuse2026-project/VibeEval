"""One model turn over explicitly supplied text, with no executable capabilities."""

from collections.abc import Sequence
from typing import Protocol

from app.llm.client import ChatInput, ChatResult, LLMClient, Message
from app.llm.config import load_settings


class TextModel(Protocol):
    async def chat(self, body: ChatInput) -> ChatResult: ...


def prepare_chat(
    prompt: str,
    *,
    history: Sequence[Message] = (),
    system_prompt: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> ChatInput:
    """Return inspectable model input without configuration or network access.

    Callers supply authorized history in order; history is neither discovered nor
    modified here. Unset model parameters are resolved by the client at call time.
    """
    messages = []
    if system_prompt is not None:
        messages.append(Message(role="system", content=system_prompt))
    messages.extend(message.model_copy(deep=True) for message in history)
    messages.append(Message(role="user", content=prompt))
    return ChatInput(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def chat_only(
    prompt: str,
    *,
    history: Sequence[Message] = (),
    system_prompt: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    client: TextModel | None = None,
) -> ChatResult:
    """Prepare input and return one text reply; command-like text stays text."""
    body = prepare_chat(
        prompt,
        history=history,
        system_prompt=system_prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    provider = client if client is not None else LLMClient(load_settings())
    return await provider.chat(body)
