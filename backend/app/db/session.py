"""Lazy engine creation and explicit commit/rollback boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from .config import get_database_url


def create_db_engine(url: URL | str | None = None) -> Engine:
    return create_engine(
        url if url is not None else get_database_url(),
        pool_pre_ping=True,
        hide_parameters=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(create_db_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Commit on success; roll back and close the session on any exception."""
    selected_factory = factory if factory is not None else get_session_factory()
    with selected_factory.begin() as session:
        yield session
