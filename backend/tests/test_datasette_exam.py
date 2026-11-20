"""Exercise the two question packages through the existing v2 HTTP contract."""

import json
import tempfile
import unittest
from pathlib import Path

import test_exam_flow_http as http

from examples.datasette_exam import TASKS, load_exam


class DatasetteExamTests(unittest.TestCase):
    def test_code_stages_publish_own_question_and_submit_own_snapshot(self):
        # Reuse the existing HTTP integration setup, including Cookie exchange.
        harness = http.FlowHttpTests()
        self.addCleanup(harness.doCleanups)
        harness.setUp()
        token, _ = harness.entry.invite(
            "datasette", "Candidate", "datasette", "Exam", 90, 24
        )
        harness.client.cookies.clear()
        harness.client.get(http.ENTRY + "/current")
        admitted = harness.client.post(
            http.ENTRY + "/verify-token", json={"token": token}
        )
        self.assertEqual(admitted.status_code, 200)
        harness.sid = admitted.json()["session_id"]
        harness.exchange = f"{http.ENTRY}/{harness.sid}/exam-access"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = {}
            for index, (task, _, _) in enumerate(TASKS, start=1):
                folder = root / task
                (folder / "workspace").mkdir(parents=True)
                (folder / "workspace" / "README.md").write_text(task, encoding="utf-8")
                (folder / "manifest.json").write_text(
                    json.dumps(
                        {"task_version": task, "files": ["README.md"], "writable": []}
                    ),
                    encoding="utf-8",
                )
                images[task] = "sha256:" + str(index) * 64
            questions, specs, definition = load_exam(root, images)

        self.assertEqual(
            [s.phases[0].duration_seconds for s in definition.stages], [3000, 2400]
        )
        self.assertNotEqual(specs[TASKS[0][0]].image_ref, specs[TASKS[1][0]].image_ref)
        harness.flow.questions = questions
        harness.flow.attach(harness.sid, definition)
        state = harness.begin()
        first_phase = state["current_phase"]["id"]
        artifacts = []
        for index, (task, _, _) in enumerate(TASKS):
            state = harness.client.get("/api/v2/attempts/current").json()
            stage_id = state["current_stage"]["id"]
            if index:
                response = harness.command(
                    f"/api/v2/stages/{stage_id}/start", "start-facet"
                )
                self.assertEqual(response.status_code, 200, response.text)
                state = response.json()
                self.assertNotEqual(state["current_phase"]["id"], first_phase)
                stale = harness.client.get(f"/api/v2/phases/{first_phase}/questions")
                self.assertIn(stale.status_code, (403, 409), stale.text)
            phase = state["current_phase"]["id"]
            self.assertIn("use_agent", state["capabilities"])
            response = harness.client.get(f"/api/v2/phases/{phase}/questions")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["items"][0]["body"], task)
            submitted = harness.command(
                f"/api/v2/stages/{stage_id}/submit", f"submit-{task}", summary=task
            )
            self.assertEqual(submitted.status_code, 200, submitted.text)
            self.assertEqual(
                harness.client.get(f"/api/v2/stages/{stage_id}/submission").json(),
                submitted.json(),
            )
            saved = harness.flow.export_submission(harness.sid, stage_id)["content"]
            self.assertEqual(saved["summary"]["value"], task)
            artifacts.append(saved["artifact"]["workspace_id"])
        self.assertEqual(len(set(artifacts)), 2)
        self.assertEqual(harness.flow.current(harness.sid)["status"], "completed")
