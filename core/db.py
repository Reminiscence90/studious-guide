"""Database engine, session handling and initialisation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from core.models import DEFAULT_TAGS, Account, Base, Tag

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRADE_JOURNAL_DATA_DIR", PROJECT_ROOT / "data"))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DEFAULT_DB_URL = f"sqlite:///{DATA_DIR / 'journal.db'}"


def database_url() -> str:
    """Database URL from ``TRADE_JOURNAL_DB_URL``, defaulting to a local SQLite file."""
    return os.environ.get("TRADE_JOURNAL_DB_URL", DEFAULT_DB_URL)


@cache
def get_engine(url: str | None = None) -> Engine:
    """Create (once per URL) the SQLAlchemy engine."""
    url = url or database_url()
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine | None = None) -> Engine:
    """Create tables and default rows (a default MYR account and the default tags)."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.scalar(select(Account.id).limit(1)) is None:
            session.add(Account(name="Main", currency="MYR"))
        if session.scalar(select(Tag.id).limit(1)) is None:
            for category, names in DEFAULT_TAGS.items():
                session.add_all(Tag(name=name, category=category) for name in names)
        session.commit()
    return engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
