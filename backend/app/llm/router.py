"""Examiner-only configuration status, model discovery and text completion."""

from fastapi import APIRouter, Depends, HTTPException

from app.admin.router import current_session
from app.core.security import require_browser_header

from .client import ChatInput, ChatResult, LLMClient, ModelInfo
from .config import LLMError, LLMSettings, load_settings

router = APIRouter(
    prefix="/api/v1/admin/llm",
    tags=["模型 API"],
    dependencies=[Depends(current_session)],
)


def get_settings() -> LLMSettings:
    try:
        return load_settings()
    except LLMError as error:
        raise HTTPException(error.status, str(error)) from None


def get_client(settings: LLMSettings = Depends(get_settings)) -> LLMClient:
    return LLMClient(settings)


@router.get("/config")
def configuration(settings: LLMSettings = Depends(get_settings)):
    return {
        "configured": bool(settings.api_key),
        "base_url": settings.base_url,
        "model": settings.model or None,
        "timeout_seconds": settings.timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
    }


@router.get("/models", response_model=list[ModelInfo])
async def models(client: LLMClient = Depends(get_client)):
    try:
        return await client.models()
    except LLMError as error:
        raise HTTPException(error.status, str(error)) from None


@router.post(
    "/chat",
    response_model=ChatResult,
    dependencies=[Depends(require_browser_header)],
)
async def chat(body: ChatInput, client: LLMClient = Depends(get_client)):
    try:
        return await client.chat(body)
    except LLMError as error:
        raise HTTPException(error.status, str(error)) from None
