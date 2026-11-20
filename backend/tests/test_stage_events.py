"""A cached event page must not bypass phase revocation between frames."""

import asyncio
import unittest

from app.agent_integration.event_stream import EventStream, EventStreamError


class PhaseEventTests(unittest.TestCase):
    def test_revoke_between_two_buffered_events_stops_the_second_event(self):
        async def exercise():
            revoked = False

            async def access():
                if revoked:
                    raise EventStreamError("AGENT_ACCESS_REVOKED", 403)

            async def page(after, limit):
                return {
                    "head_sequence": 2,
                    "closed": False,
                    "items": [
                        {
                            "sequence": i,
                            "kind": "fixture",
                            "data": {"message": f"message{i}"},
                            "created_at": 1,
                        }
                        for i in (1, 2)
                    ],
                }

            async def disconnected():
                return False

            stream = EventStream(page, access, disconnected, check_each_event=True)
            initial = await stream.prepare("conversation", 0)
            chunks = []
            async for chunk in stream.body("conversation", 0, initial, follow=False):
                chunks.append(chunk)
                if b"message1" in chunk:
                    revoked = True
            content = b"".join(chunks)
            self.assertIn(b"message1", content)
            self.assertNotIn(b"message2", content)
            self.assertIn(b"AGENT_ACCESS_REVOKED", content)

        asyncio.run(exercise())
