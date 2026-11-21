"""Assemble the four-stage candidate exam from versioned public materials."""

import hashlib
import json
import re
from pathlib import Path

from app.exam_flow import ExamDefinition, PublicQuestion
from app.exam_flow.types import PhaseDefinition, StageDefinition, digest
from app.stage_resources import ResourceSpec
from examples.datasette_exam import load_exam

PACKAGE = Path(__file__).resolve().parents[2] / "problems" / "tinydb-pilot-v1"
MANIFEST = PACKAGE.parents[1] / "maintainer/tinydb-pilot/package-manifest-v1.json"
INTERVIEW_DRAFT = Path(__file__).with_name("r2-interview-draft.json")
REPORT_FIELDS = ("location", "trigger", "actual", "expected", "cause")


def source_files(folder):
    """Use the package allowlist rather than including an entire offline workspace."""
    prefix = folder + "/repository/"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {
        path[len(folder) + 1 :]: sha
        for path, sha in manifest["files"].items()
        if path.startswith(prefix)
    }


def checked_source(folder, path, sha):
    # The handoff hashes were computed on LF bytes. Windows checkouts may use CRLF.
    data = (PACKAGE / folder / path).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError(
            f"TinyDB source does not match the published manifest: {folder}/{path}"
        )
    return data


class Questions:
    def __init__(self, code_questions):
        self.code_questions = code_questions
        self.items = {}

    def register(self, version, items):
        reference = digest([item.model_dump(mode="json") for item in items])
        self.items[version, reference] = items
        return reference

    def questions(self, context, reference):
        items = self.items.get((context.task_version, reference))
        return (
            items
            if items is not None
            else self.code_questions.questions(context, reference)
        )

    def validate_answer(self, context, question, value):
        schema = question.answer_schema
        if schema.get("type") == "string":
            if not isinstance(value, str) or len(value) > schema["maxLength"]:
                raise ValueError("answer exceeds the text limit")
        elif schema.get("type") == "object" and question.id == "findings":
            if not isinstance(value, dict) or set(value) != {"conclusion", "reports"}:
                raise ValueError("invalid report")
            conclusion, reports = value["conclusion"], value["reports"]
            if (
                conclusion not in ("found", "none", "undecided")
                or not isinstance(reports, list)
                or len(reports) > 5
            ):
                raise ValueError("invalid report count or conclusion")
            if conclusion == "none" and reports:
                raise ValueError("no findings cannot contain reports")
            for report in reports:
                if not isinstance(report, dict) or set(report) != set(REPORT_FIELDS):
                    raise ValueError("invalid report fields")
                if (
                    not all(isinstance(text, str) for text in report.values())
                    or sum(map(len, report.values())) > 1200
                ):
                    raise ValueError("invalid report length")
        else:
            self.code_questions.validate_answer(context, question, value)


def text_question(key, title, body):
    return PublicQuestion(
        id=key,
        title=title,
        body=body,
        answer_schema={"type": "string", "maxLength": 1500},
    )


def assemble(root, images, interview_path):
    legacy, specs, code_exam = load_exam(root, images)
    provider = Questions(legacy)
    interview = json.loads(interview_path.read_text(encoding="utf-8"))
    source = (PACKAGE / "02-answer/QUESTIONS.md").read_text(encoding="utf-8")
    five = [
        text_question("q" + number, title, body.strip())
        for number, title, body in re.findall(
            r"^## Q(\d+)：(.*?)（20 分）\n(.*?)(?=^## |\Z)", source, re.M | re.S
        )
    ]
    if [q.id for q in five] != [f"q{i}" for i in range(1, 6)]:
        raise ValueError("invalid TinyDB question package")
    review = (
        (PACKAGE / "03-review/REVIEW.md")
        .read_text(encoding="utf-8")
        .replace("在 FINDINGS.md", "在审阅报告中")
    )
    stages = []
    for key, title, folder in (
        ("tinydb-understanding", "仓库理解", "01-explore"),
        ("tinydb-review", "代码审阅", "03-review"),
    ):
        version = key + "-v1"
        files = source_files(folder)
        for path, sha in files.items():
            checked_source(folder, path, sha)
        specs[key] = ResourceSpec(
            task_version=version,
            image_ref=images[key],
            files=tuple(files),
            file_sha256=files,
        )
        if key == "tinydb-understanding":
            phases = (
                PhaseDefinition(
                    key="explore",
                    title="仓库探索",
                    duration_seconds=1200,
                    capabilities={
                        "read_repository",
                        "read_notes",
                        "edit_notes",
                        "use_agent",
                    },
                ),
                PhaseDefinition(
                    key="answer",
                    title="独立答题",
                    duration_seconds=900,
                    capabilities={"read_repository", "read_notes", "edit_answers"},
                    question_ref=provider.register(version, five),
                ),
            )
        else:
            report = PublicQuestion(
                id="findings",
                title="独立代码审阅",
                body=review,
                answer_schema={
                    "type": "object",
                    "properties": {
                        "conclusion": {"enum": ["found", "none", "undecided"]},
                        "reports": {
                            "type": "array",
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    k: {"type": "string"} for k in REPORT_FIELDS
                                },
                                "required": list(REPORT_FIELDS),
                                "additionalProperties": False,
                            },
                            "x-maxCharactersPerReport": 1200,
                        },
                    },
                    "required": ["conclusion", "reports"],
                    "additionalProperties": False,
                },
            )
            phases = (
                PhaseDefinition(
                    key="review",
                    title="独立审阅",
                    duration_seconds=1200,
                    capabilities={"read_repository", "edit_answers"},
                    question_ref=provider.register(version, [report]),
                ),
            )
        stages.append(
            StageDefinition(
                key=key,
                title=title,
                task_version=version,
                resource_ref=key,
                submission_kind="text",
                phases=phases,
            )
        )
    if set(interview) != {s.key for s in code_exam.stages}:
        raise ValueError("interview must cover both coding stages")
    for stage in code_exam.stages:
        bodies = interview[stage.key]
        if (
            not isinstance(bodies, list)
            or len(bodies) != 2
            or not all(isinstance(b, str) and b.strip() for b in bodies)
        ):
            raise ValueError(
                "two complete interview questions are required per coding stage"
            )
        items = [
            text_question(f"q{i}", f"第{i}问", body) for i, body in enumerate(bodies, 1)
        ]
        phase = PhaseDefinition(
            key="interview",
            title="独立问答",
            duration_seconds=600,
            capabilities={"read_repository", "read_frozen_code", "edit_answers"},
            question_ref=provider.register(stage.task_version, items),
        )
        stages.append(
            StageDefinition(
                **{
                    **stage.model_dump(),
                    "phases": (*stage.phases, phase),
                    "summary_limit": 0,
                }
            )
        )
    version = digest([s.model_dump(mode="json") for s in stages])
    exam = ExamDefinition(
        key="r2-four-stage", version=version, title="四题编程能力考试", stages=stages
    )
    return provider, specs, exam
