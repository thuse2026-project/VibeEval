"""只读公开测试：检查业务行为，不依赖服务的内部存储实现。"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from booking import BookingService


def at(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-01T{value}")


class BookingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = BookingService()

    def reserve_allowed(self, room: str, start: str, end: str):
        try:
            return self.service.reserve(room, at(start), at(end))
        except ValueError as error:
            self.fail(f"此预约按业务规则应成功，但被拒绝：{error}")

    def test_valid_reservation(self):
        first = self.reserve_allowed(" A101 ", "09:00", "10:00")
        second = self.reserve_allowed("A101", "11:00", "12:00")
        self.assertTrue(first.booking_id)
        self.assertNotEqual(first.booking_id, second.booking_id)
        self.assertEqual((first.room_id, first.start, first.end), ("A101", at("09:00"), at("10:00")))
        self.assertFalse(first.cancelled)

    def test_overlapping_reservation_rejected(self):
        self.reserve_allowed("A101", "09:00", "10:00")
        for start, end in (("09:30", "10:30"), ("08:30", "09:30"), ("09:15", "09:45"),
                           ("08:00", "11:00"), ("09:00", "10:00")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.service.reserve("A101", at(start), at(end))

    def test_adjacent_reservations_allowed(self):
        self.reserve_allowed("A101", "09:00", "10:00")
        second = self.reserve_allowed("A101", "10:00", "11:00")
        self.assertEqual(second.start, at("10:00"))

    def test_different_rooms_independent(self):
        first = self.reserve_allowed("A101", "09:00", "10:00")
        second = self.reserve_allowed("B202", "09:00", "10:00")
        self.assertNotEqual(first.booking_id, second.booking_id)
        self.assertEqual(second.room_id, "B202")

    def test_cancel_releases_room(self):
        first = self.reserve_allowed("A101", "09:00", "10:00")
        cancelled = self.service.cancel(first.booking_id)
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(cancelled.booking_id, first.booking_id)
        second = self.reserve_allowed("A101", "09:00", "10:00")
        self.assertNotEqual(second.booking_id, first.booking_id)
        self.assertFalse(second.cancelled)

    def test_invalid_input_rejected(self):
        for start, end in (("10:00", "10:00"), ("11:00", "10:00")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.service.reserve("A101", at(start), at(end))
        with self.assertRaises(ValueError):
            self.service.reserve(" ", at("09:00"), at("10:00"))
        with self.assertRaises(ValueError):
            self.service.reserve("A101", "09:00", "10:00")
        with self.assertRaises(ValueError):
            self.service.reserve("A101", at("09:00").replace(tzinfo=timezone.utc), at("10:00"))

    def test_unknown_booking_cancel_rejected(self):
        with self.assertRaises(ValueError):
            self.service.cancel("does-not-exist")

    def test_repeated_cancel_is_idempotent(self):
        first = self.reserve_allowed("A101", "09:00", "10:00")
        once = self.service.cancel(first.booking_id)
        twice = self.service.cancel(first.booking_id)
        self.assertTrue(twice.cancelled)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
