"""Examiner-facing read contract; the submission owner supplies the reader."""

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, ValidationError, field_validator

from .router import current_session


class SubmissionSummary(BaseModel):
    submission_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: str = Field(min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=120)
    candidate_name: str = Field(min_length=1, max_length=120)
    exam_id: str = Field(min_length=1, max_length=120)
    exam_title: str = Field(min_length=1, max_length=120)
    submitted_at: int = Field(ge=0)
    admitted_at: int | None = Field(default=None, ge=0)
    started_at: int | None = Field(default=None, ge=0)


class SnapshotFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (value.startswith("/") or "\\" in value or ":" in value or "\x00" in value
                or any(part in ("", ".", "..") for part in value.split("/"))):
            raise ValueError("Snapshot paths must be relative POSIX file paths")
        return value


class SubmissionDetail(SubmissionSummary):
    snapshot_id: str = Field(min_length=1, max_length=128)
    files: list[SnapshotFile]
    public_test_output: str | None

    @field_validator("files")
    @classmethod
    def unique_paths(cls, value: list[SnapshotFile]) -> list[SnapshotFile]:
        if len({item.path for item in value}) != len(value):
            raise ValueError("Duplicate snapshot file path")
        return value


class SubmissionView(SubmissionDetail):
    source: Literal["live", "demo"]


class SubmissionReader(Protocol):
    """Read committed records only; no freeze, execution, or persistence is done here."""

    def list_submissions(self) -> list[SubmissionSummary | dict]: ...

    def get_submission(self, submission_id: str) -> SubmissionDetail | dict | None: ...


def get_submission_reader() -> SubmissionReader | None:
    return None


Reader = Annotated[SubmissionReader | None, Depends(get_submission_reader)]
router = APIRouter(prefix="/api/v1/admin", tags=["答卷调阅"], dependencies=[Depends(current_session)])


def list_committed(reader: SubmissionReader) -> list[SubmissionSummary]:
    try:
        records = [SubmissionSummary.model_validate(row) for row in reader.list_submissions()]
        identities = {(row.exam_id, row.candidate_id) for row in records}
        if len(identities) != len(records) or len({row.submission_id for row in records}) != len(records):
            raise ValueError("Duplicate committed submission")
        return records
    except (ValidationError, ValueError, TypeError) as error:
        raise HTTPException(502, "提交服务返回的列表数据不完整，请联系维护者") from error
    except Exception as error:
        raise HTTPException(503, "提交数据暂时不可用，请稍后重试") from error


@router.get("/submissions/{submission_id}", response_model=SubmissionView)
def detail(reader: Reader,
           submission_id: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
           source: Literal["live", "demo"] = "live"):
    if source == "demo":
        from .demo import demo_submission
        record = demo_submission(submission_id)
    else:
        if reader is None:
            raise HTTPException(503, "真实提交服务尚未接入，当前无法读取真实答卷")
        try:
            record = reader.get_submission(submission_id)
        except Exception as error:
            raise HTTPException(503, "答卷暂时无法读取，请稍后重试") from error
    if record is None:
        raise HTTPException(404, "未找到该提交记录，请检查链接或返回列表")
    try:
        validated = SubmissionDetail.model_validate(record)
        if validated.submission_id != submission_id:
            raise ValueError("Mismatched submission identity")
        return SubmissionView(**validated.model_dump(), source=source)
    except (ValidationError, ValueError, TypeError) as error:
        raise HTTPException(502, "提交服务返回的答卷数据不完整，请联系维护者") from error
