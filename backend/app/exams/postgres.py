"""Admission uses the same committed PostgreSQL records as the sandbox lifecycle."""

import secrets
import time

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from ..db.models import ExamSession, Invitation
from ..db.session import get_session_factory
from .store import RECOVERY_SECONDS, EntryError, digest, validate_invitation


class PostgresEntryStore:
    def __init__(self, factory=None):
        self._factory = factory

    @property
    def factory(self):
        try:
            return self._factory or get_session_factory()
        except ValueError as error:
            raise HTTPException(503, "共享数据库配置不完整，请联系维护者") from error

    def invite(self, candidate_id, name, exam_id, title, duration_minutes, valid_hours):
        validate_invitation(
            candidate_id, name, exam_id, title, duration_minutes, valid_hours
        )
        token = "tok_" + secrets.token_urlsafe(32)
        expires = int(time.time()) + valid_hours * 3600
        try:
            with self.factory.begin() as db:
                db.add(
                    Invitation(
                        token_hash=digest(token),
                        candidate_id=candidate_id.strip(),
                        candidate_name=name.strip(),
                        exam_id=exam_id.strip(),
                        exam_title=title.strip(),
                        duration_minutes=duration_minutes,
                        expires_at=expires,
                    )
                )
        except IntegrityError as error:
            raise ValueError(
                "该考生在该场次已有凭证，不能重复创建；请联系考官核对"
            ) from error
        return token, expires

    @staticmethod
    def public(row):
        if row.admitted_at + RECOVERY_SECONDS <= time.time():
            raise EntryError(410, "会话恢复期限已结束，请联系考官；原凭证不能重新使用")
        invitation = row.invitation
        return {
            "session_id": row.session_id,
            "status": row.status,
            "candidate": {
                "id": invitation.candidate_id,
                "name": invitation.candidate_name,
            },
            "exam_info": {
                "id": invitation.exam_id,
                "title": invitation.exam_title,
                "duration_minutes": invitation.duration_minutes,
            },
            "admitted_at": row.admitted_at,
            "started_at": row.started_at,
            "deadline_at": row.deadline_at,
            "task_version_id": row.task_version_id,
        }

    def current(self, browser):
        with self.factory() as db:
            row = db.scalar(
                select(ExamSession).where(ExamSession.browser_hash == digest(browser))
            )
            return self.public(row) if row else None

    def admit(self, token, browser):
        token_hash, browser_hash = digest(token), digest(browser)
        with self.factory.begin() as db:
            # Cross-process serialization also covers one browser claiming different invitations.
            key = int.from_bytes(bytes.fromhex(browser_hash)[:8], "big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            current = db.scalar(
                select(ExamSession).where(ExamSession.browser_hash == browser_hash)
            )
            if current:
                result = self.public(current)
                if current.invitation_hash != token_hash:
                    raise EntryError(
                        409,
                        "此浏览器已有入场会话，请恢复原会话；暂不支持切换考生或场次",
                    )
            else:
                invitation = db.scalar(
                    select(Invitation)
                    .where(Invitation.token_hash == token_hash)
                    .with_for_update()
                )
                if invitation is None:
                    raise EntryError(401, "入场凭证无效，请检查后重试")
                claimed = db.scalar(
                    select(ExamSession).where(ExamSession.invitation_hash == token_hash)
                )
                if claimed:
                    raise EntryError(
                        409, "凭证已使用，请回到原浏览器恢复会话；更换设备请联系考官"
                    )
                if invitation.expires_at <= time.time():
                    raise EntryError(410, "入场凭证已过期，请联系考官")
                row = ExamSession(
                    session_id="ses_" + secrets.token_hex(16),
                    invitation=invitation,
                    browser_hash=browser_hash,
                    admitted_at=int(time.time()),
                    status="admitted",
                )
                db.add(row)
                db.flush()
                result = self.public(row)
        # Only publish success after the transaction commits.
        return result

    def list_entries(self):
        with self.factory() as db:
            rows = db.execute(
                select(Invitation, ExamSession).outerjoin(
                    ExamSession, ExamSession.invitation_hash == Invitation.token_hash
                )
            ).all()
            return [
                dict(
                    candidate_id=i.candidate_id,
                    candidate_name=i.candidate_name,
                    exam_id=i.exam_id,
                    exam_title=i.exam_title,
                    expires_at=i.expires_at,
                    session_id=s.session_id if s else None,
                    admitted_at=s.admitted_at if s else None,
                    started_at=s.started_at if s else None,
                    status=s.status if s else None,
                )
                for i, s in rows
            ]
