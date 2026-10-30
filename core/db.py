"""Database engine, session handling and initialisation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from core.importer import BUILTIN_PRESETS, load_specs, recalculate_trades
from core.instruments import DEFAULT_INSTRUMENTS, STABLE_QUOTES, resolve
from core.models import DEFAULT_TAGS, Account, Base, BrokerPreset, Instrument, Tag, Trade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRADE_JOURNAL_DATA_DIR", PROJECT_ROOT / "data"))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DEFAULT_DB_URL = f"sqlite:///{DATA_DIR / 'journal.db'}"


def database_url() -> str:
    """Database URL from ``TRADE_JOURNAL_DB_URL``, defaulting to a local SQLite file."""
    return os.environ.get("TRADE_JOURNAL_DB_URL", DEFAULT_DB_URL)


def get_engine(url: str | None = None) -> Engine:
    """The SQLAlchemy engine for ``url`` (default: :func:`database_url`), created once per URL."""
    return _engine_for(url or database_url())


@cache
def _engine_for(url: str) -> Engine:
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
    """Create tables and default rows: a USD account, the default tags, and the built-in
    instruments and broker presets that are missing (so ones added in a later version
    reach existing journals; edited ones are kept)."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    with Session(engine) as session:
        if session.scalar(select(Account.id).limit(1)) is None:
            session.add(Account(name="Main"))
        if session.scalar(select(Tag.id).limit(1)) is None:
            for category, names in DEFAULT_TAGS.items():
                session.add_all(Tag(name=name, category=category) for name in names)
        presets = set(session.scalars(select(BrokerPreset.name)))
        session.add_all(
            BrokerPreset(name=name, mapping=dict(mapping), datetime_format=fmt)
            for name, (mapping, fmt) in BUILTIN_PRESETS.items()
            if name not in presets
        )
        have = set(session.scalars(select(Instrument.symbol)))
        missing = [spec for spec in DEFAULT_INSTRUMENTS if spec.symbol not in have]
        session.add_all(Instrument.from_spec(spec) for spec in missing)
        session.flush()
        if missing and have:
            # Trades in the new instruments get their market and point value.
            recalculate_trades(session)
        else:
            # Pairs not quoted in USD take their point value from the broker's USD P&L;
            # recalculating them keeps journals from older versions up to date.
            specs = load_specs(session)
            crosses = [
                symbol
                for symbol in session.scalars(select(Trade.symbol).distinct())
                if resolve(symbol, specs).quote_currency not in STABLE_QUOTES
            ]
            if crosses:
                recalculate_trades(session, crosses)
        session.commit()
    return engine


# Columns added after the first release: (table, column, DDL type). ``create_all`` only
# creates missing tables, so existing databases get these with ALTER TABLE.
_LATE_COLUMNS = [
    ("executions", "raw_symbol", "VARCHAR(32)"),
    ("executions", "order_id", "VARCHAR(64)"),
    ("executions", "position_id", "VARCHAR(64)"),
    ("executions", "realized_pnl", "FLOAT"),
    ("executions", "realized_net_pnl", "FLOAT"),
]


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    columns: dict[str, set[str]] = {}
    with engine.begin() as conn:
        for table, column, ddl in _LATE_COLUMNS:
            if table not in columns:
                columns[table] = {c["name"] for c in inspector.get_columns(table)}
            if column not in columns[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


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
