"""Provider-specific preview settings leave other deployments unchanged."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agent_integration.window import STAGE_AGENT_WINDOW
from app.llm.config import LLMSettings
from examples.datasette_preview import create_preview


class PreviewSettingsTests(unittest.TestCase):
    def test_only_official_flash_uses_thinking_and_larger_window(self):
        for url, model, expected in (
            ("https://api.deepseek.com", "deepseek-flash", True),
            ("https://provider.test", "deepseek-flash", False),
            ("https://api.deepseek.com", "another-model", False),
        ):
            with self.subTest(url=url, model=model):
                resources = SimpleNamespace(
                    agent_window_policy=STAGE_AGENT_WINDOW, settings=None
                )
                app = MagicMock(
                    state=SimpleNamespace(
                        stage_resources=resources,
                        exam_flow=object(),
                        datasette_definition=object(),
                        entry=SimpleNamespace(factory=lambda: None),
                    )
                )
                settings = LLMSettings(url, "fixture-key", model)
                with (
                    patch("examples.datasette_preview.create_app", return_value=app),
                    patch("examples.datasette_preview.StaticFiles"),
                    patch(
                        "examples.datasette_preview.load_settings",
                        return_value=settings,
                    ),
                ):
                    create_preview()
                if expected:
                    self.assertTrue(resources.settings.thinking)
                    self.assertEqual(resources.settings.max_output_tokens, 8192)
                    self.assertEqual(
                        resources.agent_window_policy.context_window_tokens, 131072
                    )
                else:
                    self.assertIsNone(resources.settings)
                    self.assertIs(resources.agent_window_policy, STAGE_AGENT_WINDOW)
