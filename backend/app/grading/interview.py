"""Validate private grading-worker deliveries against sealed R2 interview materials."""

import json
import time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.db.models import StageGradingJob
from app.exam_flow.types import Identifier, digest
from app.grading.stage_jobs import create_job


class Record(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def valid_text(value):
    if "\0" in value:
        raise ValueError("Invalid text character")
    value.encode("utf-8")
    return value


Text = Annotated[
    str, Field(min_length=1, max_length=2000, pattern=r"\S"), AfterValidator(valid_text)
]
Points = Annotated[float, Field(ge=0, le=10)]


class Criterion(Record):
    maximum: int = Field(ge=1, le=10)
    anchor: Text
    evidence_required: bool


class Rubric(Record):
    version: Identifier
    prompt_version: Identifier
    task_version: Identifier
    questions_sha256: Hash
    questions: dict[Identifier, dict[Identifier, Criterion]]

    @model_validator(mode="after")
    def check_totals(self):
        if set(self.questions) != {"q1", "q2"} or any(
            sum(c.maximum for c in criteria.values()) != 10
            for criteria in self.questions.values()
        ):
            raise ValueError("Each interview question needs a ten-point rubric")
        return self


class FileRef(Record):
    path: Text
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)


class EventRef(Record):
    event_id: Identifier


class Mark(Record):
    criterion_id: Identifier
    maximum: int
    score: Points | None
    reason: Text
    answer_quote: Annotated[str, Field(max_length=2000), AfterValidator(valid_text)]
    evidence: list[FileRef | EventRef] = Field(max_length=32)
    needs_review: bool
    review_reason: Text | None


class QuestionMarks(Record):
    question_id: Identifier
    marks: list[Mark] = Field(min_length=1, max_length=10)
    score: Points | None


class Suggestion(Record):
    questions: list[QuestionMarks] = Field(min_length=2, max_length=2)
    score: float | None = Field(ge=0, le=20)


class Delivery(Record):
    # Created by the trusted caller, independently of the model's response text.
    input_sha256: Hash
    attempt: int = Field(ge=1, le=2)
    model: Text
    reasoning_effort: Text
    max_output_tokens: int = Field(ge=1)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    transient_error: Literal["timeout", "provider_unavailable"] | None = None
    response: str | None = None

    @model_validator(mode="after")
    def check_outcome(self):
        if (self.response is None) == (self.transient_error is None):
            raise ValueError("Supply one provider response or one transient error")
        return self


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("DUPLICATE_JSON_KEY")
    return result


def check_total(score, parts):
    if any(p is None for p in parts):
        if score is not None:
            raise ValueError("UNREVIEWED_TOTAL")
    elif score is None or Decimal(str(score)) != sum(Decimal(str(p)) for p in parts):
        raise ValueError("INCORRECT_TOTAL")


def score_number(token):
    value = float(token)
    if Decimal(str(value)) != Decimal(token):
        raise ValueError("Score loses precision")
    return value


def validate_response(raw, rule, answers, event_ids, files):
    if len(raw) > 200000:
        raise ValueError("RESPONSE_TOO_LONG")
    try:
        proposal = Suggestion.model_validate(
            json.loads(raw, object_pairs_hook=unique_object, parse_float=score_number)
        )
    except (ValueError, RecursionError, ArithmeticError):
        raise ValueError("INVALID_RESPONSE_FORMAT") from None
    if {q.question_id for q in proposal.questions} != set(rule.questions):
        raise ValueError("QUESTION_SET_MISMATCH")
    for q in proposal.questions:
        criteria = rule.questions[q.question_id]
        if len(q.marks) != len(criteria) or {m.criterion_id for m in q.marks} != set(
            criteria
        ):
            raise ValueError("CRITERION_SET_MISMATCH")
        answer = answers[q.question_id]
        for mark in q.marks:
            criterion = criteria[mark.criterion_id]
            if mark.maximum != criterion.maximum or (
                mark.score is not None and mark.score > criterion.maximum
            ):
                raise ValueError("INVALID_MAXIMUM_OR_SCORE")
            if mark.needs_review != (mark.score is None) or mark.needs_review != (
                mark.review_reason is not None
            ):
                raise ValueError("INVALID_REVIEW_STATE")
            if not answer.strip():
                if mark.score != 0 or mark.answer_quote or mark.evidence:
                    raise ValueError("EMPTY_ANSWER")
            elif not mark.answer_quote.strip() or mark.answer_quote not in answer:
                raise ValueError("ANSWER_QUOTE_MISMATCH")
            elif (
                criterion.evidence_required
                and not mark.needs_review
                and not mark.evidence
            ):
                raise ValueError("EVIDENCE_REQUIRED")
            for ref in mark.evidence:
                if isinstance(ref, EventRef):
                    if ref.event_id not in event_ids:
                        raise ValueError("UNKNOWN_EVENT")
                elif (
                    ref.path not in files
                    or not ref.first_line <= ref.last_line <= files[ref.path]
                ):
                    raise ValueError("UNKNOWN_FILE_LINES")
        check_total(q.score, [m.score for m in q.marks])
    check_total(proposal.score, [q.score for q in proposal.questions])
    return proposal.model_dump(mode="json")


def prepare(resources, exported, rule):
    content, receipt = exported["content"], exported["receipt"]
    phases = [p for p in content["phases"] if p["definition"]["key"] == "interview"]
    if (
        receipt["kind"] != "code"
        or content["task_version"] != rule.task_version
        or len(phases) != 1
    ):
        raise ValueError("INTERVIEW_RULE_MISMATCH")
    phase = phases[0]
    questions = phase["questions"]
    if (
        not questions
        or len(questions) != 2
        or {q["id"] for q in questions} != set(rule.questions)
        or digest(questions) != rule.questions_sha256
        or phase["definition"]["question_ref"] != rule.questions_sha256
    ):
        raise ValueError("FROZEN_QUESTIONS_MISMATCH")
    answers = {}
    for key in rule.questions:
        if key not in phase["answers"]:
            answers[key] = ""
            continue
        draft = phase["answers"][key]
        if (
            not isinstance(draft["value"], str)
            or type(draft["revision"]) is not int
            or draft["revision"] < 1
        ):
            raise ValueError("SAVED_ANSWER_UNAVAILABLE")
        answers[key] = draft["value"]
    event_ids = set()
    job = create_job(
        {"submission_id": receipt["id"], "stage_id": content["stage_id"]},
        exported,
        resources,
        event_ids=event_ids,
    )
    binding = {
        "submission_id": receipt["id"],
        "basis_sha256": digest(job["basis"]),
        "rule_sha256": digest(rule.model_dump(mode="json")),
        "rule_version": rule.version,
        "prompt_version": rule.prompt_version,
    }
    files = {f["path"]: len(f["content"].splitlines()) for f in exported["files"]}
    return binding, answers, event_ids, files


def record_delivery(resources, exported, rule, delivery):
    binding, answers, event_ids, files = prepare(resources, exported, rule)
    if delivery.input_sha256 != digest(binding):
        raise ValueError("STALE_INPUTS")
    fingerprint = digest(delivery.model_dump(mode="json"))
    with resources.factory.begin() as db:
        row = db.get(StageGradingJob, binding["submission_id"], with_for_update=True)
        current = row.components["answers"]
        attempts = current.get("attempts", [])
        if delivery.attempt <= len(attempts):
            if attempts[delivery.attempt - 1]["delivery_sha256"] != fingerprint:
                raise ValueError("ATTEMPT_CONFLICT")
            return current
        if "examiner_review" in row.components:
            raise ValueError("COMPONENT_CLOSED")
        if current != {"status": "pending", "score": None} and not (
            current.get("status") == "retryable"
            and current.get("binding") == binding
            and current.get("score") is None
            and len(attempts) == 1
            and "manual" not in current
        ):
            raise ValueError("COMPONENT_CLOSED")
        if delivery.attempt != len(attempts) + 1:
            raise ValueError("ATTEMPT_OUT_OF_ORDER")
        if attempts and any(
            attempts[0][key] != getattr(delivery, key)
            for key in ("model", "reasoning_effort", "max_output_tokens")
        ):
            raise ValueError("RETRY_CONFIG_MISMATCH")
        failure = delivery.transient_error
        suggestion = None
        if failure is None:
            try:
                suggestion = validate_response(
                    delivery.response, rule, answers, event_ids, files
                )
            except ValueError as error:
                failure = str(error)
        status = "needs_review"
        if failure and delivery.attempt == 1:
            status = "retryable"
        elif suggestion is not None and suggestion["score"] is not None:
            status = "suggested"
        attempt = {
            **delivery.model_dump(exclude={"response", "transient_error"}),
            "delivery_sha256": fingerprint,
            "failure": failure,
            "recorded_at": int(time.time()),
        }
        result = {
            "status": status,
            "score": None,
            "suggestion": suggestion,
            "binding": binding,
            "attempts": [*attempts, attempt],
        }
        row.components = {**row.components, "answers": result}
        return result
