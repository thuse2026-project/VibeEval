"""运行三个小场景；修复预约逻辑后，三个场景应全部通过。"""

from datetime import datetime

from booking import BookingService


def demonstrate(label: str, second_start: str | None = None, cancel_first: bool = False) -> None:
    service = BookingService()
    first = service.reserve(
        "A101", datetime.fromisoformat("2026-11-01T09:00"), datetime.fromisoformat("2026-11-01T10:00")
    )
    if cancel_first:
        service.cancel(first.booking_id)
    if second_start:
        try:
            service.reserve(
                "A101", datetime.fromisoformat(second_start),
                first.end if cancel_first else datetime.fromisoformat("2026-11-01T11:00")
            )
        except ValueError as error:
            print(f"[未通过] {label}：{error}")
            return
    print(f"[通过] {label}")


if __name__ == "__main__":
    demonstrate("创建普通预约")
    demonstrate("前一场结束后立即开始下一场", second_start="2026-11-01T10:00")
    demonstrate("取消预约后重新使用原时段", second_start="2026-11-01T09:00", cancel_first=True)
