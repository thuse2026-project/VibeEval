"""Synthetic packaging fixtures, executed over stdin only in a disposable stack."""

import json
import os
import sys
import uuid

from app.admin.accounts import accounts_path, hash_password, load_accounts
from app.db.config import get_database_url
from app.db.lifecycle import SessionLifecycle
from app.exams.postgres import PostgresEntryStore
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile

if os.environ.get(
    "VIBEEVAL_DEPLOYMENT_SMOKE"
) != "1" or not get_database_url().database.startswith("vibeeval_compose_"):
    raise SystemExit("Only disposable deployment smoke databases are supported")

if sys.argv[1] == "prepare":
    suffix = uuid.uuid4().hex
    username = "smoke_" + suffix
    password = "deployment-smoke-password"
    accounts = load_accounts()
    accounts[username] = {
        "name": "Smoke examiner",
        "password_hash": hash_password(password),
    }
    accounts_path().write_text(json.dumps(accounts), encoding="utf-8")
    token, _ = PostgresEntryStore().invite(
        "smoke_" + suffix, "Smoke candidate", "smoke", "Packaging smoke", 60, 1
    )
    print(json.dumps({"username": username, "password": password, "token": token}))
elif sys.argv[1] == "persist":
    # This is a synthetic, already-frozen file set, never a claimed sandbox run.
    session_id = sys.argv[2]
    lifecycle = SessionLifecycle()
    image = "sha256:" + "d" * 64
    lifecycle.prepare_sandbox(session_id, image, task_version_id="deployment:smoke")
    with lifecycle.lock_session(session_id) as locked:
        locked.bind_sandbox("smoke-container", image_ref=image)
        locked.mark_ready()
    lifecycle.start_exam(session_id)
    lifecycle.begin_submission(session_id)
    receipt = SubmissionService().persist_frozen_submission(
        session_id,
        "deployment-smoke",
        [SnapshotFile("answer.py", "# persisted smoke\n")],
    )
    print(json.dumps(receipt))
else:
    raise SystemExit("Unknown fixture operation")
