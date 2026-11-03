"""Workspace reads serialized with submission; revisions belong to shared DB."""

import time
from hashlib import sha256

from .files import ALLOWED_FILES, EDITABLE, encode_content, validate_path


class RevisionStoreUnavailable(RuntimeError):
    pass


class SaveConflict(ValueError):
    def __init__(self, version):
        self.version = version
        super().__init__("file changed; reload before saving")


class WorkspaceReader:
    def __init__(self, lifecycle, sandbox):
        self.lifecycle = lifecycle
        self.sandbox = sandbox

    @staticmethod
    def revision_writer(locked):
        # D supplies this on LockedSession in the same transaction. Do not open
        # another session or invent an in-memory version when it is unavailable.
        method = getattr(locked, "observe_file_revision", None)
        if not callable(method):
            raise RevisionStoreUnavailable("persistent file revisions not configured")
        return method

    def read(self, session_id, path):
        validate_path(path)
        with self.lifecycle.lock_session(session_id) as locked:
            view = locked.require_writable()
            observe = self.revision_writer(locked)
            content = self.sandbox.read_file(view, path)
            version = observe(path, sha256(encode_content(content)).hexdigest())
            result = {"path": path, "content": content, "version": version}
        return result

    def tree(self, session_id):
        with self.lifecycle.lock_session(session_id) as locked:
            view = locked.require_writable()
            observe = self.revision_writer(locked)
            contents = self.sandbox.read_files(view)
            if set(contents) != ALLOWED_FILES:
                raise ValueError("sandbox manifest mismatch")
            files = [
                {
                    "path": path,
                    "is_dir": False,
                    "version": observe(
                        path, sha256(encode_content(content)).hexdigest()
                    ),
                    "readonly": path not in EDITABLE,
                }
                for path, content in sorted(contents.items())
            ]
        return {"files": files}

    def save(self, session_id, path, expected_version, content):
        validate_path(path, write=True)
        data = encode_content(content)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        conflict = None
        with self.lifecycle.lock_session(session_id) as locked:
            view = locked.require_writable()
            observe = self.revision_writer(locked)
            current = self.sandbox.read_file(view, path)
            digest = sha256(encode_content(current)).hexdigest()
            version = observe(path, digest)
            if version != expected_version:
                conflict = version
            else:
                locked.require_writable()
                result = self.sandbox.save_file(view, path, digest, content)
                if not result["saved"]:
                    conflict = observe(
                        path, sha256(encode_content(result["content"])).hexdigest()
                    )
                else:
                    version = observe(path, sha256(data).hexdigest())
                    saved_at = int(time.time())
        # Commit observed terminal edits even when returning HTTP 409.
        if conflict is not None:
            raise SaveConflict(conflict)
        return {"path": path, "new_version": version, "saved_at": saved_at}
