"""Run alongside the candidate API. Only grade submissions from the selected edition."""

import argparse
import logging
import time
from types import SimpleNamespace

from sqlalchemy import select

from app.admin import four_stage, stage_evaluation
from app.db.models import ExamFlowAttempt, StageGradingJob
from examples.datasette_exam import create_app


def sweep(app):
    factory = app.state.stage_resources.factory
    version = app.state.datasette_definition.version
    request = SimpleNamespace(app=app)
    with factory() as db:
        states = db.scalars(
            select(ExamFlowAttempt.state_json).where(
                ExamFlowAttempt.state_json["definition"]["version"].astext == version
            )
        ).all()
        pending = []
        for state in states:
            for stage in state["stages"]:
                receipt = stage.get("receipt")
                if not receipt:
                    continue
                sid = receipt["id"]
                job = db.get(StageGradingJob, sid)
                components = job.components if job else {}
                if "examiner_review" in components:
                    continue
                objective = (
                    receipt["kind"] == "code"
                    and components.get("objective", {}).get("status") != "completed"
                )
                subjective = components.get("answers", {}).get("status", "pending") in {
                    "pending",
                    "retryable",
                }
                if objective or subjective:
                    pending.append((sid, objective, subjective))
    for sid, objective, subjective in pending:
        if objective:
            try:
                stage_evaluation.grade(request, sid)
            except Exception as exc:
                logging.warning(
                    "Objective component unavailable for %s (%s)",
                    sid,
                    type(exc).__name__,
                )
        if subjective:
            try:
                four_stage.suggest(request, sid)
            except Exception as exc:
                logging.warning(
                    "Subjective component unavailable for %s (%s)",
                    sid,
                    type(exc).__name__,
                )
    return len(pending)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    app = create_app()
    while True:
        sweep(app)
        if args.once:
            break
        time.sleep(60)
