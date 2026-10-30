"""Local entry persistence, isolated from the future PostgreSQL business models."""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DEFAULT_DATABASE = Path(__file__).resolve().parents[3] / "runtime/entry.sqlite3"
RECOVERY_SECONDS = 30 * 24 * 60 * 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class EntryError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class EntryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("ENTRY_DB_FILE", str(DEFAULT_DATABASE)))

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS invitations (
                token_hash TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                candidate_name TEXT NOT NULL,
                exam_id TEXT NOT NULL,
                exam_title TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                expires_at INTEGER NOT NULL,
                UNIQUE(exam_id, candidate_id)
            );
            CREATE TABLE IF NOT EXISTS entry_sessions (
                session_id TEXT PRIMARY KEY,
                invitation_hash TEXT NOT NULL UNIQUE REFERENCES invitations(token_hash),
                browser_hash TEXT NOT NULL UNIQUE,
                admitted_at INTEGER NOT NULL
            );
        """)
        return connection

    def invite(self, candidate_id: str, name: str, exam_id: str, title: str,
               duration_minutes: int, valid_hours: int) -> tuple[str, int]:
        if not all(value.strip() for value in (candidate_id, name, exam_id, title)):
            raise ValueError("考生与考核信息不能为空")
        if any(len(value) > 120 for value in (candidate_id, name, exam_id, title)):
            raise ValueError("每个文本字段不能超过 120 字符")
        if not 1 <= duration_minutes <= 1440 or not 1 <= valid_hours <= 8760:
            raise ValueError("考试时长为 1～1440 分钟，入场期限为 1～8760 小时")
        token = "tok_" + secrets.token_urlsafe(32)
        expires = int(time.time()) + valid_hours * 3600
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO invitations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (digest(token), candidate_id.strip(), name.strip(), exam_id.strip(),
                     title.strip(), duration_minutes, expires),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("该考生在该场次已有凭证，不能重复创建；请联系考官核对") from error
        return token, expires

    @staticmethod
    def find(connection, clause: str, value: str):
        return connection.execute(
            "SELECT s.*, i.candidate_id, i.candidate_name, i.exam_id, "
            "i.exam_title, i.duration_minutes FROM entry_sessions s "
            "JOIN invitations i ON i.token_hash = s.invitation_hash WHERE " + clause,
            (value,),
        ).fetchone()

    @staticmethod
    def public(row) -> dict:
        return {
            "session_id": row["session_id"], "status": "admitted",
            "candidate": {"id": row["candidate_id"], "name": row["candidate_name"]},
            "exam_info": {"id": row["exam_id"], "title": row["exam_title"],
                          "duration_minutes": row["duration_minutes"]},
            "admitted_at": row["admitted_at"], "started_at": None, "deadline_at": None,
        }

    def current(self, browser: str) -> dict | None:
        with closing(self.connect()) as connection:
            row = self.find(connection, "s.browser_hash = ?", digest(browser))
            if row and row["admitted_at"] + RECOVERY_SECONDS <= time.time():
                raise EntryError(410, "会话恢复期限已结束，请联系考官；原凭证不能重新使用")
            return self.public(row) if row else None

    def admit(self, token: str, browser: str) -> dict:
        # The UNIQUE constraints + transaction make retries/concurrent claims one admission.
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            token_hash = digest(token)
            current = self.find(connection, "s.browser_hash = ?", digest(browser))
            if current:
                if current["admitted_at"] + RECOVERY_SECONDS <= time.time():
                    raise EntryError(410, "会话恢复期限已结束，请联系考官；原凭证不能重新使用")
                if current["invitation_hash"] == token_hash:
                    return self.public(current)
                raise EntryError(409, "此浏览器已有入场会话，请恢复原会话；暂不支持切换考生或场次")
            invitation = connection.execute(
                "SELECT * FROM invitations WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if not invitation:
                raise EntryError(401, "入场凭证无效，请检查后重试")
            claimed = self.find(connection, "s.invitation_hash = ?", token_hash)
            if claimed:
                raise EntryError(409, "凭证已使用，请回到原浏览器恢复会话；更换设备请联系考官")
            if invitation["expires_at"] <= time.time():
                raise EntryError(410, "入场凭证已过期，请联系考官")
            session_id = "ses_" + secrets.token_hex(16)
            connection.execute(
                "INSERT INTO entry_sessions VALUES (?, ?, ?, ?)",
                (session_id, token_hash, digest(browser), int(time.time())),
            )
            return self.public(self.find(connection, "s.session_id = ?", session_id))
