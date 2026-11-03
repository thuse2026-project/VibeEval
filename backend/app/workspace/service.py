"""Workspace reads serialized with submission; revisions belong to shared DB."""

from hashlib import sha256

from .files import ALLOWED_FILES, EDITABLE, encode_content, validate_path


class RevisionStoreUnavailable(RuntimeError):
    pass


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
