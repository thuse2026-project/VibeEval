"""Cursor scope, SSE framing, bounded tailing and cancellation contracts."""

import asyncio
import json
import unittest

from app.agent_integration.event_stream import (
    MAX_SEQUENCE,
    EventStream,
    EventStreamError,
    EventStreamPolicy,
    event_cursor,
    frame,
    resolve_cursor,
)


def decode_frames(chunks):
    result = []
    for block in b"".join(chunks).decode("utf-8").split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if line and not line.startswith(":") and ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.lstrip(" ")
        if "data" in fields:
            fields["data"] = json.loads(fields["data"])
            result.append(fields)
    return result


class Fixture:
    def __init__(self, count=1):
        self.items = [
            {
                "sequence": n,
                "kind": "fixture.event",
                "created_at": n,
                "data": {"number": n},
            }
            for n in range(1, count + 1)
        ]
        self.now, self.calls, self.checks = 0.0, 0, 0
        self.closed, self.revoked, self.gone = False, False, False
        self.on_page, self.on_sleep = None, None

    async def page(self, after, limit):
        self.calls += 1
        if self.on_page:
            self.on_page(self.calls)
        return {
            "items": [item for item in self.items if item["sequence"] > after][:limit],
            "head_sequence": len(self.items),
            "closed": self.closed,
        }

    async def access(self):
        self.checks += 1
        if self.revoked:
            raise EventStreamError("AGENT_ACCESS_REVOKED", 404)

    async def disconnected(self):
        return self.gone

    async def sleep(self, duration):
        self.now += duration
        if self.on_sleep:
            self.on_sleep()

    def stream(self, **options):
        policy = EventStreamPolicy(
            max_seconds=0.2, heartbeat_seconds=0.1, poll_seconds=0.05, **options
        )
        return EventStream(
            self.page,
            self.access,
            self.disconnected,
            policy=policy,
            clock=lambda: self.now,
            sleep=self.sleep,
        )


class EventProtocolTests(unittest.TestCase):
    def test_scoped_cursors_roundtrip_unicode_and_reconnect_header_wins(self):
        cid = "会话:🙂"
        cursor = event_cursor(cid, MAX_SEQUENCE)
        self.assertEqual(resolve_cursor(cid, 0, cursor), MAX_SEQUENCE)
        self.assertEqual(resolve_cursor(cid, 2), 2)
        self.assertEqual(resolve_cursor(cid), 0)
        with self.assertRaisesRegex(EventStreamError, "SCOPE_MISMATCH"):
            resolve_cursor("another", last_event_id=cursor)

    def test_malformed_or_noncanonical_cursors_fail_before_reading(self):
        for value in (
            "123",
            "v2.YQ.1",
            "v1.YQ.01",
            "v1.YQ.-1",
            "v1.YR.1",
            "v1.____.1",
            "v1.YQ.9223372036854775808",
            "v1.YQ.1\n",
            "x" * 1025,
        ):
            with self.subTest(value=value), self.assertRaises(EventStreamError):
                resolve_cursor("a", last_event_id=value)
        for number in (True, -1, MAX_SEQUENCE + 1, 1.5):
            with self.assertRaises(EventStreamError):
                resolve_cursor("a", number)

    def test_frame_escapes_text_into_one_data_line_without_forging_ids(self):
        data = {"text": "\n\nid: injected\r\x00\u2028🙂"}
        chunk = frame("agent.event", data, cursor=event_cursor("c", 1))
        self.assertEqual(decode_frames([chunk])[0]["data"], data)
        self.assertEqual(chunk.count(b"\ndata:"), 1)
        self.assertEqual(
            chunk.count(b"id:"), 2
        )  # One field and one escaped JSON value.
        with self.assertRaises(ValueError):
            frame("bad\nevent", {})

    def test_policy_rejects_unbounded_or_inconsistent_server_limits(self):
        for options in (
            {"page_size": 201},
            {"max_events": True},
            {"max_seconds": 31},
            {"max_bytes": 100},
            {"heartbeat_seconds": 0},
            {"poll_seconds": float("nan")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EventStreamPolicy(**options)


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, source, *, follow=True, after=0, **options):
        stream = source.stream(**options)
        initial = await stream.prepare("c", after)
        return [
            chunk async for chunk in stream.body("c", after, initial, follow=follow)
        ]

    async def test_replay_stops_at_initial_head_while_new_events_are_committed(self):
        source = Fixture(2)

        def append(number):
            if number == 2:
                source.items.append(
                    {"sequence": 3, "kind": "later", "data": {}, "created_at": 3}
                )

        source.on_page = append
        result = decode_frames(await self.collect(source, follow=False, page_size=1))
        self.assertEqual(
            [item["data"]["sequence"] for item in result if "id" in item], [1, 2]
        )
        self.assertEqual(result[-1]["data"]["code"], "REPLAY_COMPLETE")

    async def test_follow_delivers_a_later_commit_and_heartbeats_have_no_id(self):
        source = Fixture()

        def append():
            if len(source.items) == 1:
                source.items.append(
                    {"sequence": 2, "kind": "later", "data": {}, "created_at": 2}
                )

        source.on_sleep = append
        chunks = await self.collect(source)
        result = decode_frames(chunks)
        self.assertEqual(
            [item["data"]["sequence"] for item in result if "id" in item], [1, 2]
        )
        self.assertIn(b": keep-alive\n\n", chunks)
        self.assertEqual(result[-1]["data"]["next_sequence"], 2)
        self.assertNotIn("id", result[-1])

    async def test_count_budget_cursor_resumes_before_the_unemitted_event(self):
        source = Fixture(3)
        first = decode_frames(await self.collect(source, follow=False, max_events=1))
        self.assertEqual(first[-1]["data"]["code"], "CONNECTION_BUDGET_REACHED")
        self.assertEqual(first[-1]["data"]["next_sequence"], 1)
        after = resolve_cursor("c", last_event_id=first[-1]["data"]["cursor"])
        second = decode_frames(await self.collect(source, follow=False, after=after))
        self.assertEqual(
            [item["data"]["sequence"] for item in second if "id" in item], [2, 3]
        )

    async def test_byte_budget_does_not_advance_over_an_unsent_record(self):
        source = Fixture(3)
        for item in source.items:
            item["data"] = {"text": "x" * 450}
        result = decode_frames(
            await self.collect(
                source, follow=False, max_bytes=1024, max_frame_bytes=1024
            )
        )
        sent = [item for item in result if "id" in item]
        self.assertEqual(len(sent), 1)
        self.assertEqual(result[-1]["data"]["next_sequence"], 1)

    async def test_close_drains_committed_events_and_then_stops_without_retry(self):
        source = Fixture(2)
        source.closed = True
        result = decode_frames(await self.collect(source, page_size=1))
        self.assertEqual(
            [item["data"]["sequence"] for item in result if "id" in item], [1, 2]
        )
        self.assertEqual(result[-1]["event"], "stream.closed")
        self.assertFalse(result[-1]["data"]["retryable"])

    async def test_revocation_stops_before_fetching_the_next_batch(self):
        source = Fixture()
        source.on_sleep = lambda: setattr(source, "revoked", True)
        result = decode_frames(await self.collect(source))
        self.assertEqual(result[-1]["data"]["code"], "AGENT_ACCESS_REVOKED")
        self.assertEqual(source.calls, 1)

    async def test_disconnection_stops_reads_and_cancellation_is_not_a_tool_failure(
        self,
    ):
        source = Fixture()
        source.gone = True
        result = decode_frames(await self.collect(source))
        self.assertFalse(any("id" in item for item in result))
        self.assertEqual(source.calls, 1)
        source = Fixture()
        entered = asyncio.Event()

        async def block(_):
            entered.set()
            await asyncio.Event().wait()

        source.sleep = block
        task = asyncio.create_task(self.collect(source))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(source.calls, 1)

    async def test_midstream_private_failure_is_sanitized_with_last_sent_cursor(self):
        source = Fixture()

        def fail(number):
            if number > 1:
                raise RuntimeError("private SQL and credential")

        source.on_page = fail
        chunks = await self.collect(source)
        self.assertNotIn(b"private", b"".join(chunks))
        result = decode_frames(chunks)
        self.assertEqual(result[-1]["event"], "stream.error")
        self.assertEqual(result[-1]["data"]["next_sequence"], 1)

    async def test_oversized_first_event_fails_before_stream_and_later_event_keeps_cursor(
        self,
    ):
        source = Fixture()
        source.items[0]["data"] = {"text": "x" * 2000}
        with self.assertRaisesRegex(EventStreamError, "TOO_LARGE"):
            await self.collect(source, max_frame_bytes=512)
        source = Fixture()
        source.on_sleep = lambda: (
            source.items.append(
                {
                    "sequence": 2,
                    "kind": "large",
                    "data": {"text": "x" * 2000},
                    "created_at": 2,
                }
            )
            if len(source.items) == 1
            else None
        )
        result = decode_frames(await self.collect(source, max_frame_bytes=512))
        self.assertEqual(result[-1]["data"]["code"], "AGENT_EVENT_TOO_LARGE")
        self.assertEqual(result[-1]["data"]["next_sequence"], 1)

    async def test_noncontiguous_batch_is_reported_without_skipping(self):
        source = Fixture(2)
        source.items[1]["sequence"] = 3
        result = decode_frames(await self.collect(source))
        self.assertEqual(result[-1]["data"]["code"], "AGENT_EVENT_GAP")
        self.assertEqual(result[-1]["data"]["next_sequence"], 1)
