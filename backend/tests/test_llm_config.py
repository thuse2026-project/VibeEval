import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.llm.config import LLMError, load_settings


class LLMConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        self.enterContext(patch("app.llm.config.ENV_FILE", self.env_file))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_missing_optional_config_does_not_prevent_startup(self):
        settings = load_settings()
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.model, "")
        self.assertEqual(settings.base_url, "https://openrouter.ai/api/v1")

    def test_file_settings_and_environment_precedence(self):
        self.env_file.write_text(
            "LLM_API_KEY='file-secret'\nLLM_MODEL=file/model\n"
            "LLM_BASE_URL=https://provider.test/v1/\n"
            "LLM_TIMEOUT_SECONDS=20\nLLM_MAX_OUTPUT_TOKENS=100\n",
            encoding="utf-8",
        )
        settings = load_settings()
        self.assertEqual(settings.api_key, "file-secret")
        self.assertEqual(settings.base_url, "https://provider.test/v1")
        self.assertEqual(settings.model, "file/model")
        self.assertEqual(settings.timeout_seconds, 20)
        self.assertEqual(settings.max_output_tokens, 100)
        os.environ.update(LLM_API_KEY="process-secret", LLM_MODEL="process/model")
        self.assertEqual(load_settings().api_key, "process-secret")
        self.assertEqual(load_settings().model, "process/model")
        os.environ["LLM_API_KEY"] = ""
        self.assertEqual(load_settings().api_key, "")

    def test_secret_is_not_interpolated_or_in_repr(self):
        self.env_file.write_text(
            "LLM_API_KEY='key-${HOME}-literal'\n", encoding="utf-8"
        )
        settings = load_settings()
        self.assertEqual(settings.api_key, "key-${HOME}-literal")
        self.assertNotIn("key-${HOME}-literal", repr(settings))

    def test_invalid_urls_and_secret_in_url_are_rejected_without_echo(self):
        for value in (
            "file:///etc/passwd",
            "missing-host",
            "https://key-secret@provider.test/v1",
            "https://provider.test/v1?key=key-secret",
            "https://provider.test/v1#fragment",
            "https://provider.test:bad/v1",
            "https://provider.test:99999/v1",
            "https://provider.test/v1/chat/completions",
            "https://provider.test/v1/models",
            "https://provider.test/white space",
        ):
            with self.subTest(value=value):
                os.environ["LLM_BASE_URL"] = value
                with self.assertRaises(LLMError) as caught:
                    load_settings()
                self.assertNotIn("key-secret", str(caught.exception))

    def test_invalid_limits_and_keys_are_rejected(self):
        for key, values in (
            ("LLM_TIMEOUT_SECONDS", ["0", "121", "nan", "inf", "invalid"]),
            ("LLM_MAX_OUTPUT_TOKENS", ["0", "4097", "1.5", "invalid"]),
            ("LLM_API_KEY", ["key secret", "key\nsecret", "密钥"]),
        ):
            for value in values:
                with (
                    self.subTest(key=key, value=value),
                    patch.dict(os.environ, {key: value}),
                ):
                    with self.assertRaises(LLMError):
                        load_settings()
