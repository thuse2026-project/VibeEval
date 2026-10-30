"""Stable, explicitly synthetic data. Never reads a candidate's active workspace."""

import json
from pathlib import Path

# Fixed timestamps keep list rows, detail links, and repeated reads consistent.
DEMO_TIME = 1793347200


def demo_records() -> list[dict]:
    records = []
    for index in range(12):
        status = ("invited", "admitted", "in_progress", "submitted")[index % 4]
        exam = "A" if index < 6 else "B"
        records.append({
            "candidate_id": f"demo-{index + 1:03d}", "candidate_name": f"演示考生 {index + 1:02d}",
            "exam_id": f"demo-exam-{exam}", "exam_title": f"演示场次 {exam}", "status": status,
            "session_id": None if status == "invited" else f"demo-session-{index + 1}",
            "admitted_at": None if status == "invited" else DEMO_TIME - 7200 - index * 60,
            "started_at": DEMO_TIME - 6000 - index * 60 if status in ("in_progress", "submitted") else None,
            "submission_id": f"demo-submission-{index + 1}" if status == "submitted" else None,
            "submitted_at": DEMO_TIME - 600 - index * 60 if status == "submitted" else None,
        })
    return records


def demo_submission(submission_id: str) -> dict | None:
    row = next((row for row in demo_records() if row["submission_id"] == submission_id), None)
    if row is None:
        return None
    files = json.loads(Path(__file__).with_name("demo_snapshot.json").read_text(encoding="utf-8"))
    return {
        **row,
        "snapshot_id": submission_id.replace("demo-submission-", "demo-snapshot-"),
        "files": files,
        "public_test_output": (
            "演示输出（虚构记录，非本次考试执行结果）\n"
            "会议室预约样题原始版本：\n"
            "test_adjacent_reservations_allowed ... FAIL\n"
            "test_cancel_releases_room ... FAIL\n"
            "其余 6 项 ... OK\n"
            "Ran 8 tests\nFAILED (failures=2)\n"
        ),
    }
