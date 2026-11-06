"""会议室预约核心逻辑。请根据题目说明修复现有行为。"""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import uuid4


@dataclass(frozen=True)
class Booking:
    booking_id: str
    room_id: str
    start: datetime
    end: datetime
    cancelled: bool = False


class BookingService:
    def __init__(self) -> None:
        self._bookings: dict[str, Booking] = {}

    def reserve(self, room_id: str, start: datetime, end: datetime) -> Booking:
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValueError("会议室编号不能为空")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise ValueError("开始和结束时间必须是 datetime")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ValueError("请使用不带时区的本地时间")
        if start >= end:
            raise ValueError("开始时间必须早于结束时间")
        room_id = room_id.strip()

        for booking in self._bookings.values():
            if booking.room_id == room_id:
                if start <= booking.end and booking.start <= end:
                    raise ValueError("该会议室在此时段已有预约")

        booking = Booking(uuid4().hex, room_id, start, end)
        self._bookings[booking.booking_id] = booking
        return booking

    def cancel(self, booking_id: str) -> Booking:
        if booking_id not in self._bookings:
            raise ValueError("预约不存在")
        booking = replace(self._bookings[booking_id], cancelled=True)
        self._bookings[booking_id] = booking
        return booking
