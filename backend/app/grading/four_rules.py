"""Private marking keys. Never include these records in the candidate definition."""

import json
from pathlib import Path

from app.exam_flow.types import digest
from app.grading.interview import Rubric

ROOT = Path(__file__).resolve().parents[3] / "maintainer/tinydb-pilot"
INTERVIEW = {
    "column-statistics": [
        [
            ("goal", 4, "说明核心使用目标与约束"),
            ("choice", 4, "结合实际方案解释取舍的理由"),
            ("check", 2, "指出本人的核对及对应证据"),
        ],
        [
            ("coverage", 4, "把实际验证与业务要求对应"),
            ("risk", 3, "解释现有验证的局限及关键风险"),
            ("followup", 3, "给出有明确预期的补充验证"),
        ],
    ],
    "facet-pagination": [
        [
            ("trigger", 4, "说清触发条件、因果链和用户现象"),
            ("cause", 4, "解释根因及修复位置"),
            ("check", 2, "说明本人怎样核对根因及依据"),
        ],
        [
            ("coverage", 4, "说明实际验证怎样检出原问题"),
            ("risk", 3, "识别伪修复或相邻行为回归"),
            ("followup", 3, "给出区分方法与明确预期"),
        ],
    ],
}
PROCESS = [
    ("direction", 3, "理解任务并选择实现方向"),
    ("judgment", 4, "核对依据并作独立判断"),
    ("feedback", 3, "验证并根据结果反馈。纯手工操作同样可得满分"),
]
REVIEW = {
    "location": "准确定位",
    "trigger": "必要触发条件",
    "actual_expected": "实际与预期",
    "cause": "解释根因",
}


def material(content, stage_key):
    phase = next(
        p
        for p in content["phases"]
        if "edit_answers" in p["definition"]["capabilities"]
    )
    answers = {
        q["id"]: phase["answers"].get(q["id"], {}).get("value", "")
        for q in phase["questions"]
    }
    lines = []

    def line(key, label, maximum, answer_id, group="answers"):
        lines.append(
            dict(
                key=key, label=label, maximum=maximum, answer_id=answer_id, group=group
            )
        )

    interview = None
    if stage_key in INTERVIEW:
        sections = {}
        for q, criteria in zip(phase["questions"], INTERVIEW[stage_key], strict=True):
            sections[q["id"]] = {}
            for key, maximum, label in criteria:
                line(q["id"] + "." + key, label, maximum, q["id"])
                sections[q["id"]][key] = dict(
                    anchor=label,
                    maximum=maximum,
                    evidence_required=key in {"check", "coverage"},
                )
        interview = Rubric(
            version="r2-interview-v1",
            prompt_version="r2-subjective-v1",
            task_version=content["task_version"],
            questions_sha256=digest(phase["questions"]),
            questions=sections,
        )
        for key, maximum, label in PROCESS:
            line("process." + key, label, maximum, "", "process")
    else:
        rules = json.loads((ROOT / "rubric-v1.json").read_bytes())
        if stage_key == "tinydb-understanding":
            for q in rules["onboarding"]:
                for c in q["criteria"]:
                    line(c["id"], c["description"], c["points"], q["id"].lower())
        elif stage_key == "tinydb-review":
            for r in rules["review"]:
                for c in r["criteria"]:
                    line(
                        r["id"] + "." + c["id"],
                        r["reference"] + " · " + REVIEW[c["id"]],
                        c["points"],
                        "findings",
                    )
        else:
            raise ValueError("Unknown four-stage marking key")
    return phase["questions"], answers, lines, interview
