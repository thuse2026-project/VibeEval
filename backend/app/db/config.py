"""Read database settings independently of the shell's working directory."""

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def get_database_url() -> URL:
    values = {**dotenv_values(ENV_FILE, interpolate=False), **os.environ}
    explicit_url = values.get("DATABASE_URL")
    if explicit_url:
        try:
            url = make_url(explicit_url)
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL is not a valid PostgreSQL URL") from None
        if url.drivername not in ("postgresql", "postgresql+psycopg"):
            raise ValueError("DATABASE_URL must use postgresql or postgresql+psycopg")
        url = url.set(drivername="postgresql+psycopg")
    else:
        try:
            port = int(values.get("POSTGRES_PORT", "5432"))
        except (TypeError, ValueError):
            raise ValueError(
                "POSTGRES_PORT must be an integer from 1 to 65535"
            ) from None
        url = URL.create(
            "postgresql+psycopg",
            username=values.get("POSTGRES_USER", "vibeeval"),
            password=values.get("POSTGRES_PASSWORD"),
            host=values.get("POSTGRES_HOST", "127.0.0.1"),
            port=port,
            database=values.get("POSTGRES_DB", "vibeeval"),
        )
    if not (url.username and url.password and url.host and url.database):
        raise ValueError(
            "Database user, password, host and database must be configured"
        )
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Database port must be an integer from 1 to 65535")
    return url
