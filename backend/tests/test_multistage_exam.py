import unittest

from app.exams.multistage import ExamAttempt, PhaseKind, StageStatus


class MultiStageExamTests(unittest.TestCase):
    def test_full_attempt_keeps_server_deadlines_and_advances_in_order(self):
        attempt = ExamAttempt(admitted_at=100)
        attempt.prepare()
        attempt.start_stage(100)
        self.assertEqual(attempt.stage.phase.deadline_at, 1300)
        self.assertTrue(attempt.capabilities()["use_agent"])
        attempt.finish_phase(1300, "timeout")
        attempt.submit_stage(1300)
        self.assertEqual(attempt.current_stage, 2)
        self.assertEqual(attempt.stage.status, StageStatus.READY)
        self.assertEqual(attempt.stage.definition.kind, PhaseKind.ANSWERING)

    def test_answering_phase_cannot_edit_repository_or_use_agent(self):
        attempt = ExamAttempt(admitted_at=0)
        attempt.prepare()
        attempt.start_stage(1)
        attempt.finish_phase(2)
        attempt.submit_stage(2)
        attempt.start_stage(3)
        capabilities = attempt.capabilities()
        self.assertFalse(capabilities["write_repository"])
        self.assertFalse(capabilities["use_terminal"])
        self.assertTrue(capabilities["edit_answers"])

    def test_invalid_transition_does_not_change_state(self):
        attempt = ExamAttempt(admitted_at=0)
        with self.assertRaises(ValueError):
            attempt.start_stage(0)
        self.assertEqual((attempt.status, attempt.current_stage), ("admitted", 1))


if __name__ == "__main__":
    unittest.main()
