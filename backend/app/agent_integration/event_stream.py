"""Read-only, bounded SSE replay/tailing over committed conversation events."""

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass

from app.agent.session_types import SessionError

from .store import ConversationNotFound

MAX_SEQUENCE = 2**63 - 1


class EventStreamError(SessionError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class EventStreamPolicy:
    page_size: int = 50
    max_events: int = 500
    max_bytes: int = 1024 * 1024
    max_frame_bytes: int = 128 * 1024
    max_seconds: float = 25
    poll_seconds: float = 0.5
    heartbeat_seconds: float = 10

    def __post_init__(self):
        if not (
            all(
                type(value) is int
                for value in (
                    self.page_size,
                    self.max_events,
                    self.max_bytes,
                    self.max_frame_bytes,
                )
            )
            and 1 <= self.page_size <= 200
            and 1 <= self.max_events <= 1000
            and 1024 <= self.max_bytes <= 4 * 1024 * 1024
            and 512 <= self.max_frame_bytes <= self.max_bytes
            and 0
            < self.poll_seconds
            <= self.heartbeat_seconds
            <= self.max_seconds
            <= 30
        ):
            raise ValueError("invalid event stream bounds")


def event_cursor(conversation_id, sequence):
    if (
        not isinstance(conversation_id, str)
        or not 1 <= len(conversation_id) <= 128
        or any(c.isspace() or c == "\x00" for c in conversation_id)
    ):
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
    try:
        identity = (
            base64.urlsafe_b64encode(conversation_id.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
    except UnicodeError:
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422) from None
    return f"v1.{identity}.{sequence}"


def resolve_cursor(conversation_id, after_sequence=None, last_event_id=None):
    if after_sequence is not None and (
        type(after_sequence) is not int or not 0 <= after_sequence <= MAX_SEQUENCE
    ):
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
    if not last_event_id:
        return after_sequence or 0
    if len(last_event_id) > 1024 or not re.fullmatch(
        r"v1\.[A-Za-z0-9_-]+\.(?:0|[1-9][0-9]{0,18})", last_event_id
    ):
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
    _, encoded_id, number = last_event_id.split(".")
    try:
        decoded_id = base64.b64decode(
            encoded_id + "=" * (-len(encoded_id) % 4), altchars=b"-_", validate=True
        ).decode("utf-8")
        sequence = int(number)
        canonical = event_cursor(decoded_id, sequence)
    except (ValueError, UnicodeError):
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422) from None
    if canonical != last_event_id:
        raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
    if decoded_id != conversation_id:
        raise EventStreamError("AGENT_EVENT_CURSOR_SCOPE_MISMATCH")
    # Standard reconnect headers take precedence over a URL's initial position.
    return sequence


def frame(kind, data, *, cursor=None):
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", kind)
        or cursor is not None
        and any(c in cursor for c in "\r\n\x00")
    ):
        raise ValueError("invalid SSE field")
    body = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return (
        (f"id: {cursor}\n" if cursor else "") + f"event: {kind}\ndata: {body}\n\n"
    ).encode("utf-8")


class EventStream:
    def __init__(
        self,
        page,
        access,
        disconnected,
        *,
        policy=None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.page, self.access, self.disconnected = page, access, disconnected
        self.policy, self.clock, self.sleep = (
            policy or EventStreamPolicy(),
            clock,
            sleep,
        )

    async def prepare(self, conversation_id, after):
        await self.access()
        value = await self.page(after, self.policy.page_size)
        # Fail before response headers for an invalid initial stored event.
        for item in value["items"]:
            if (
                len(
                    frame(
                        "agent.event",
                        {"version": 1, "conversation_id": conversation_id, **item},
                        cursor=event_cursor(conversation_id, item["sequence"]),
                    )
                )
                > self.policy.max_frame_bytes
            ):
                raise EventStreamError("AGENT_EVENT_TOO_LARGE")
        return value

    async def body(self, conversation_id, after, initial, *, follow=True):
        policy, current = self.policy, after
        started, heartbeat = self.clock(), self.clock()
        count, size, page = 0, 0, initial

        def end(code, retryable, kind="stream.end"):
            return frame(
                kind,
                {
                    "code": code,
                    "retryable": retryable,
                    "next_sequence": current,
                    "cursor": event_cursor(conversation_id, current),
                },
            )

        yield b"retry: 1000\n\n"
        yield frame(
            "stream.ready",
            {
                "conversation_id": conversation_id,
                "after_sequence": current,
                "head_sequence": initial["head_sequence"],
                "follow": follow,
            },
        )
        try:
            while True:
                if await self.disconnected():
                    return
                await self.access()
                if not follow:
                    page = {
                        **page,
                        "head_sequence": initial["head_sequence"],
                        "closed": initial["closed"],
                        "items": [
                            item
                            for item in page["items"]
                            if item["sequence"] <= initial["head_sequence"]
                        ],
                    }
                for item in page["items"]:
                    if await self.disconnected():
                        return
                    if self.clock() - started >= policy.max_seconds:
                        yield end("CONNECTION_WINDOW_ENDED", True)
                        return
                    sequence = item["sequence"]
                    if sequence != current + 1:
                        raise EventStreamError("AGENT_EVENT_GAP")
                    chunk = frame(
                        "agent.event",
                        {"version": 1, "conversation_id": conversation_id, **item},
                        cursor=event_cursor(conversation_id, sequence),
                    )
                    if len(chunk) > policy.max_frame_bytes:
                        raise EventStreamError("AGENT_EVENT_TOO_LARGE")
                    if (
                        count >= policy.max_events
                        or size + len(chunk) > policy.max_bytes
                    ):
                        yield end("CONNECTION_BUDGET_REACHED", True)
                        return
                    yield chunk
                    current, count, size = sequence, count + 1, size + len(chunk)
                if current < page["head_sequence"]:
                    page = await self.page(current, policy.page_size)
                    continue
                if page["closed"]:
                    yield end("AGENT_SESSION_CLOSED", False, "stream.closed")
                    return
                if not follow:
                    yield end("REPLAY_COMPLETE", False)
                    return
                now = self.clock()
                if now - started >= policy.max_seconds:
                    yield end("CONNECTION_WINDOW_ENDED", True)
                    return
                if now - heartbeat >= policy.heartbeat_seconds:
                    yield b": keep-alive\n\n"
                    heartbeat = now
                await self.sleep(
                    min(policy.poll_seconds, policy.max_seconds - (now - started))
                )
                await self.access()
                page = await self.page(current, policy.page_size)
        except EventStreamError as error:
            yield end(error.code, False, "stream.error")
        except ConversationNotFound:
            yield end("AGENT_CONVERSATION_UNAVAILABLE", False, "stream.error")
        except Exception:
            # No raw SQL, credentials, provider payload or traceback on the wire.
            yield end("AGENT_EVENT_SOURCE_UNAVAILABLE", True, "stream.error")


async def threaded(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)
