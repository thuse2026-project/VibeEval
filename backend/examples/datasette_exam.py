"""Assemble a two-stage review exam using the existing v2 backend."""

import argparse
import json
import os
from pathlib import Path
from threading import Event

from app.db.models import StageWorkspace
from app.exam_flow import ExamDefinition, PublicQuestion
from app.stage_resources import ResourceSpec
from examples.exam_flow import build_stage_app, maintain

TASKS = (
    ("column-statistics", "业务拓展 · 补齐列统计", 50),
    ("facet-pagination", "历史 Bug 修复 · 筛选与分页", 40),
)


class Questions:
    def __init__(self, items):
        self.items = items

    def questions(self, context, reference):
        return [self.items[context.task_version, reference]]

    def validate_answer(self, context, question, value):
        raise ValueError(
            "This stage accepts a code snapshot and implementation summary"
        )


def load_exam(root, images):
    questions, specs, stages = {}, {}, []
    for task, title, minutes in TASKS:
        task_root = root / task
        manifest = json.loads((task_root / "manifest.json").read_text(encoding="utf-8"))
        spec = ResourceSpec.model_validate({**manifest, "image_ref": images[task]})
        specs[task] = spec
        questions[spec.task_version, task] = PublicQuestion(
            id=task,
            title=title,
            body=(task_root / "workspace" / "README.md").read_text(encoding="utf-8"),
        )
        stages.append(
            {
                "key": task,
                "title": title,
                "task_version": spec.task_version,
                "resource_ref": task,
                "submission_kind": "code",
                "phases": [
                    {
                        "key": "coding",
                        "title": title,
                        "duration_seconds": minutes * 60,
                        "capabilities": [
                            "read_repository",
                            "write_repository",
                            "use_terminal",
                            "use_agent",
                        ],
                        "question_ref": task,
                    }
                ],
            }
        )
    definition = ExamDefinition.model_validate(
        {
            "key": "datasette-code-review",
            "version": "v1",
            "title": "Datasette 阶段三、四联测",
            "transition_seconds": 60,
            "stages": stages,
        }
    )
    return Questions(questions), specs, definition


def create_app():
    root = Path(os.environ["VIBEEVAL_DATASETTE_CONTEXTS"])
    images = {
        "column-statistics": os.environ["VIBEEVAL_DATASETTE_COLUMN_IMAGE"],
        "facet-pagination": os.environ["VIBEEVAL_DATASETTE_FACET_IMAGE"],
    }
    questions, specs, definition = load_exam(root, images)
    app = build_stage_app(questions, specs)
    app.state.datasette_definition = definition
    return app


def export_code_submission(flow, session_id, stage_id):
    result = flow.export_submission(session_id, stage_id)
    artifact = result["content"]["artifact"]
    resources = flow.resources
    with resources.factory() as db:
        row = db.get(StageWorkspace, artifact["workspace_id"])
        if (
            row is None
            or row.session_id != session_id
            or row.stage_id != stage_id
            or row.status != "sealed"
            or row.artifact_json is None
            or row.artifact_json["sha256"] != artifact["sha256"]
        ):
            raise ValueError("Submission snapshot does not match the sealed workspace")
        snapshot = resources.snapshots.read(
            artifact["sha256"],
            size_bytes=row.artifact_json["size_bytes"],
            file_count=row.artifact_json["file_count"],
        )
    result["files"] = [
        {"path": entry.path, "content": entry.content} for entry in snapshot.files
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("maintain", "attach", "export"))
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("stage_id", nargs="?")
    args = parser.parse_args()
    if args.action in ("attach", "export") and not args.session_id:
        parser.error("session_id is required")
    if args.action == "export" and not args.stage_id:
        parser.error("stage_id is required")
    app = create_app()
    flow = app.state.exam_flow
    if args.action == "maintain":
        maintain(flow, Event())
    elif args.action == "attach":
        result = flow.attach(args.session_id, app.state.datasette_definition)
        print(json.dumps(result))
    else:
        result = export_code_submission(flow, args.session_id, args.stage_id)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
