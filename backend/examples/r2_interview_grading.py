"""Inspect the input binding or record a trusted interview-worker delivery."""

import argparse
import json
from pathlib import Path

from app.exam_flow.types import FlowError, digest
from app.grading.interview import Delivery, Rubric, prepare, record_delivery
from examples.datasette_exam import create_app, export_code_submission


def export_interview_submission(flow, session_id, stage_id):
    exported = export_code_submission(flow, session_id, stage_id)
    stage = next(s for s in flow.store.load(session_id).stages if s.id == stage_id)
    # A saved draft omitted during sealing must never count as an empty answer.
    for phase in stage.phases:
        if phase.definition.key == "interview":
            sealed = next(
                (p for p in exported["content"]["phases"] if p["id"] == phase.id), None
            )
            drafts = {k: v.model_dump(mode="json") for k, v in phase.answers.items()}
            if sealed is None or sealed.get("answers") != drafts:
                raise FlowError("SEALED_ANSWERS_MISMATCH", 503)
    return exported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("stage_id")
    parser.add_argument("rule", type=Path)
    parser.add_argument("--delivery", type=Path)
    args = parser.parse_args()
    rule = Rubric.model_validate_json(args.rule.read_bytes())
    flow = create_app().state.exam_flow
    exported = export_interview_submission(flow, args.session_id, args.stage_id)
    if args.delivery:
        delivery = Delivery.model_validate_json(args.delivery.read_bytes())
        result = record_delivery(flow.resources, exported, rule, delivery)
        print(
            json.dumps(
                {"status": result["status"], "attempts": len(result["attempts"])}
            )
        )
    else:
        binding, *_ = prepare(flow.resources, exported, rule)
        print(json.dumps({**binding, "input_sha256": digest(binding)}))


if __name__ == "__main__":
    main()
