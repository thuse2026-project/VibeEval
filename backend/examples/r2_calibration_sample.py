"""Export a sealed four-stage blind marking packet, excluding model and examiner scores."""

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from app.db.models import ExamFlowAttempt
from app.exam_flow.types import EvidenceCutoff, digest
from app.grading.four_rules import material
from app.stage_resources.evidence import page
from examples.datasette_exam import create_app, export_stage_submission


def write(app, session_id, output):
    notes = [
        "# 四题独立评阅样卷",
        "",
        "请逐项填写预期得分区间、理由和出处，再与模型建议比较。",
        "",
    ]
    with app.state.stage_resources.factory() as db:
        attempt = db.scalar(
            select(ExamFlowAttempt).where(ExamFlowAttempt.session_id == session_id)
        )
        if (
            not attempt
            or attempt.state_json["status"] != "completed"
            or len(attempt.state_json["stages"]) != 4
        ):
            raise ValueError("A completed four-stage examination is required")
        entries = {}
        for index, stage in enumerate(attempt.state_json["stages"], 1):
            exported = export_stage_submission(
                app.state.exam_flow, session_id, stage["id"]
            )
            content, receipt = exported["content"], exported["receipt"]
            if digest(content) != receipt["content_sha256"]:
                raise ValueError("Receipt mismatch")
            root = f"q{index}/"
            notes += [
                "## 第%d题 · %s" % (index, stage["definition"]["title"]),
                "",
                "回执 `"
                + receipt["id"]
                + "`  ·  题包 `"
                + content["task_version"]
                + "`",
                "",
            ]
            entries[root + "receipt.json"] = json.dumps(receipt, indent=2)
            # This also retains saved notes, the original answers and phase boundaries.
            entries[root + "submission.json"] = json.dumps(
                content, ensure_ascii=False, indent=2
            )
            questions, answers, rubric, _ = material(
                content, stage["definition"]["key"]
            )
            for q in questions:
                value = answers[q["id"]]
                notes += [
                    "### " + q["title"],
                    "",
                    q["body"],
                    "",
                    "封存回答",
                    "",
                    (
                        value
                        if isinstance(value, str)
                        else json.dumps(value, ensure_ascii=False, indent=2)
                    ),
                    "",
                ]
            for file in exported.get("files", []):
                entries[root + "files/" + file["path"]] = file["content"]
            if exported.get("files"):
                artifact = content["artifact"]
                cutoff = EvidenceCutoff.model_validate(artifact["evidence"])
                offset, facts = 0, []
                while True:
                    batch, offset = page(db, cutoff, after=offset, limit=100)
                    facts.extend(batch)
                    if offset is None:
                        break
                entries[root + "evidence.json"] = json.dumps(
                    facts, ensure_ascii=False, indent=2
                )
                notes += [
                    "固定代码 `" + artifact["sha256"] + "`",
                    "",
                    f"编程记录 {len(facts)} 条。采集状态 {cutoff.completeness}。",
                    "",
                ]
            notes += [
                "| 分项 | 上限 | 预期区间 | 理由及材料位置 |",
                "|---|---:|---|---|",
            ]
            notes += [
                f"| {c['key']} · {c['label']} | {c['maximum']} | | |" for c in rubric
            ]
            notes.append("")
        entries["盲评样卷.md"] = "\n".join(notes)
    with ZipFile(output, "x", ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write(create_app(), args.session_id, args.output)
