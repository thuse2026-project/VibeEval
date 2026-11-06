from fastapi import HTTPException, Request


def require_browser_header(request: Request) -> None:
    # Cross-origin sites cannot send this header without an allowed CORS preflight.
    if request.headers.get("X-VibeEval-Request") != "1":
        raise HTTPException(403, "请求来源校验失败，请刷新页面后重试")
