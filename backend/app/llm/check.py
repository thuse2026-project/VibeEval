"""Run with python -m app.llm.check; does not require a database or browser login."""

import argparse
import asyncio

from .client import ChatInput, LLMClient, Message
from .config import LLMError, load_settings


async def check(args):
    client = LLMClient(load_settings())
    if args.list_models:
        for model in await client.models():
            print(model.id)
    else:
        result = await client.chat(
            ChatInput(
                model=args.model,
                messages=[Message(role="user", content=args.prompt)],
                max_tokens=args.max_tokens,
            )
        )
        print(result.model_dump_json(indent=2))


def main():
    parser = argparse.ArgumentParser(description="验证后端模型 API 配置")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", default="Reply with OK.")
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args()
    try:
        asyncio.run(check(args))
    except LLMError as error:
        parser.exit(1, f"模型 API 验证失败（{error.status}）：{error}\n")


if __name__ == "__main__":
    main()
