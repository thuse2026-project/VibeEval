"""Cookie-authenticated, origin-checked PTY gateway using shared session locks."""

import asyncio
import codecs
import json
import os

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.db.lifecycle import LifecycleConflict, SessionBusy, SessionLifecycle
from app.exams.router import require_candidate_session

router = APIRouter(prefix="/api/v1/sessions")
MAX_FRAME_BYTES = 16 * 1024
MAX_INPUT_BYTES = 8 * 1024


def parse_message(text):
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("terminal frame too large")
    message = json.loads(text)
    if not isinstance(message, dict):
        raise ValueError("terminal object required")
    kind = message.get("type")
    if kind == "input":
        data = message.get("data")
        if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("invalid terminal input")
        return kind, data.encode("utf-8")
    if kind == "resize":
        cols, rows = message.get("cols"), message.get("rows")
        if type(cols) is not int or type(rows) is not int:
            raise ValueError("integer terminal dimensions required")
        if not 1 <= cols <= 300 or not 1 <= rows <= 150:
            raise ValueError("terminal dimensions out of range")
        return kind, (cols, rows)
    raise ValueError("unknown terminal message")


def authorize(websocket, session_id):
    # Browser WebSocket cannot supply the HTTP custom header. An explicit Origin
    # allowlist replaces that check; Cookie ownership remains the shared check.
    origins = {
        value.strip()
        for value in os.environ.get("SANDBOX_TERMINAL_ORIGINS", "").split(",")
        if value.strip()
    }
    if websocket.headers.get("origin") not in origins:
        raise HTTPException(403, "terminal origin rejected")
    require_candidate_session(session_id, websocket)


def guarded(websocket, session_id, operation):
    authorize(websocket, session_id)
    factory = getattr(websocket.app.state.entry, "factory", None)
    if factory is None:
        raise RuntimeError("shared session store required")
    with SessionLifecycle(factory).write_guard(session_id) as view:
        return operation(view)


async def relay(websocket, terminal, check_and_run):
    async def output():
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            data = await run_in_threadpool(terminal.read)
            if data is None:
                continue
            text = decoder.decode(data, final=not data)
            if text:
                await asyncio.wait_for(
                    websocket.send_json({"type": "output", "data": text}), 5
                )
            if not data:
                return

    async def input_messages():
        while True:
            text = await websocket.receive_text()
            kind, value = parse_message(text)
            operation = (
                (lambda view: terminal.write(value))
                if kind == "input"
                else (lambda view: terminal.resize(*value))
            )
            # Busy input is never replayed: close and let the user reconnect.
            await run_in_threadpool(check_and_run, operation)

    async def state_monitor():
        while True:
            await asyncio.sleep(1)
            try:
                await run_in_threadpool(check_and_run, lambda view: None)
            except SessionBusy:
                continue

    tasks = [
        asyncio.create_task(fn()) for fn in (output, input_messages, state_monitor)
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/{session_id}/terminal")
async def terminal_endpoint(websocket: WebSocket, session_id: str):
    terminal = None
    code = 1000
    try:
        factory = getattr(websocket.app.state, "terminal_factory", None)
        if factory is None:
            raise RuntimeError("terminal is not configured")

        def check_and_run(operation):
            return guarded(websocket, session_id, operation)

        def open_terminal(view):
            nonlocal terminal
            terminal = factory.open(view)
            return terminal

        await run_in_threadpool(check_and_run, open_terminal)
        await websocket.accept()
        await relay(websocket, terminal, check_and_run)
    except WebSocketDisconnect:
        return
    except SessionBusy:
        code = 1013
    except (HTTPException, LifecycleConflict):
        code = 1008
    except (ValueError, UnicodeError, KeyError):
        code = 1008
    except Exception:
        code = 1011
    finally:
        if terminal is not None:
            try:
                await run_in_threadpool(terminal.close)
            except Exception:
                code = 1011
        try:
            await websocket.close(code=code)
        except (RuntimeError, OSError):
            pass
