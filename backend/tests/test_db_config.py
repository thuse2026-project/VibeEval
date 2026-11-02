import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.config import get_database_url


class DatabaseConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        self.env_file.write_text(
            "POSTGRES_PASSWORD=local-test-password\n", encoding="utf-8"
        )
        self.enterContext(patch("app.db.config.ENV_FILE", self.env_file))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_defaults_and_password_from_file(self):
        url = get_database_url()
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(
            (url.host, url.port, url.database, url.username),
            ("127.0.0.1", 5432, "vibeeval", "vibeeval"),
        )
        self.assertEqual(url.password, "local-test-password")

    def test_shell_settings_take_precedence(self):
        os.environ.update(
            POSTGRES_HOST="postgres",
            POSTGRES_PORT="15432",
            POSTGRES_DB="custom",
            POSTGRES_USER="user",
            POSTGRES_PASSWORD="override",
        )
        url = get_database_url()
        self.assertEqual(
            (url.host, url.port, url.database, url.username, url.password),
            ("postgres", 15432, "custom", "user", "override"),
        )

    def test_password_special_characters_are_not_interpolated_or_lost(self):
        password = "local!$@:/%[]${NOT_AN_ENV_VAR}"
        self.env_file.write_text(f"POSTGRES_PASSWORD='{password}'\n", encoding="utf-8")
        self.assertEqual(get_database_url().password, password)

    def test_configuration_does_not_depend_on_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.env_file.parent)
            self.assertEqual(get_database_url().password, "local-test-password")
        finally:
            os.chdir(previous)

    def test_explicit_url_overrides_individual_settings(self):
        os.environ["DATABASE_URL"] = (
            "postgresql://name:p%40ss%25@db:5544/custom?sslmode=require"
        )
        url = get_database_url()
        self.assertEqual(
            (url.drivername, url.password, url.host, url.port, url.database),
            ("postgresql+psycopg", "p@ss%", "db", 5544, "custom"),
        )
        self.assertEqual(url.query["sslmode"], "require")

    def test_explicit_psycopg_url_is_supported(self):
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:password@db/database"
        self.assertEqual(get_database_url().host, "db")

    def test_blank_password_in_shell_overrides_file_and_is_rejected(self):
        os.environ["POSTGRES_PASSWORD"] = ""
        with self.assertRaisesRegex(ValueError, "must be configured"):
            get_database_url()

    def test_missing_password_is_rejected(self):
        self.env_file.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be configured"):
            get_database_url()

    def test_invalid_ports_are_rejected(self):
        for port in ("", "not-a-port", "0", "65536", "-1"):
            with self.subTest(port=port):
                os.environ["POSTGRES_PORT"] = port
                with self.assertRaises(ValueError):
                    get_database_url()

    def test_other_drivers_are_rejected(self):
        os.environ["DATABASE_URL"] = "sqlite:///unexpected.db"
        with self.assertRaisesRegex(ValueError, "must use postgresql"):
            get_database_url()

    def test_invalid_explicit_url_does_not_expose_credentials(self):
        os.environ["DATABASE_URL"] = (
            "postgresql://user:private-password@host:bad-port/db"
        )
        with self.assertRaises(ValueError) as raised:
            get_database_url()
        self.assertNotIn("private-password", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
