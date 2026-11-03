"""Shared lifecycle owns state and timing; runtime owns Docker operations."""

from app.db.lifecycle import SessionBusy


class PreparationService:
    def __init__(self, lifecycle, runtime, image_ref, backend_ref, task_version_id):
        self.lifecycle, self.runtime = lifecycle, runtime
        self.image_ref, self.backend_ref = image_ref, backend_ref
        self.task_version_id = task_version_id

    def prepare(self, session_id):
        current = self.lifecycle.get_session(session_id)
        if current and current.status in {"ready", "in_progress"}:
            return current
        self.lifecycle.prepare_sandbox(
            session_id,
            self.image_ref,
            backend_ref=self.backend_ref,
            task_version_id=self.task_version_id,
        )
        try:
            with self.lifecycle.lock_session(session_id) as locked:
                view = locked.view
                if view.status in {"ready", "in_progress"}:
                    return view
                identity = self.runtime.ensure_bound(view)
                locked.bind_sandbox(identity, image_ref=view.sandbox_image_ref)
                self.runtime.check_ready(locked.view)
                result = locked.mark_ready()
            return result
        except SessionBusy:
            raise
        except Exception:
            # Previous transaction is closed before recording failure.
            try:
                with self.lifecycle.lock_session(session_id) as locked:
                    if locked.view.status == "provisioning":
                        locked.mark_provision_failed("SANDBOX_PREPARE_FAILED")
            except Exception:
                pass
            raise
