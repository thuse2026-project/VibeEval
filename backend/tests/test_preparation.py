import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from app.sandbox.preparation import PreparationService


class PreparationTests(unittest.TestCase):
    def test_ready_does_not_allocate_or_reset_clock(self):
        lifecycle, runtime = Mock(), Mock()
        lifecycle.get_session.return_value = SimpleNamespace(status="ready")
        service = PreparationService(lifecycle, runtime, "image", "node", "task")
        self.assertEqual(service.prepare("s").status, "ready")
        runtime.ensure_bound.assert_not_called()
        lifecycle.start_exam.assert_not_called()

    def test_health_failure_never_marks_ready(self):
        lifecycle, runtime, locked = Mock(), Mock(), Mock()
        lifecycle.get_session.return_value = None
        locked.view = SimpleNamespace(status="provisioning", sandbox_image_ref="image")
        lifecycle.lock_session.side_effect = lambda _: nullcontext(locked)
        runtime.check_ready.side_effect = ValueError("bad permissions")
        with self.assertRaises(ValueError):
            PreparationService(lifecycle, runtime, "image", "node", "task").prepare("s")
        locked.mark_ready.assert_not_called()
        locked.mark_provision_failed.assert_called_once_with("SANDBOX_PREPARE_FAILED")
