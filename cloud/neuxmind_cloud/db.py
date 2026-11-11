"""Database engine, sessions and the wallet-lock primitive.

Concurrency model
-----------------
Money-moving operations (reserve, settle, release, credit, adjust) always start
with :func:`lock_wallet`, which reads the org's ``wallets`` row with
``SELECT ... FOR UPDATE``. On PostgreSQL this is a real row lock: concurrent
transactions for the same org queue behind it, so the "check available, then
insert reservation" sequence cannot oversell. Different orgs never block each
other.

SQLite has no row locks and SQLAlchemy drops ``FOR UPDATE`` for it. Instead we
open every SQLite transaction with ``BEGIN IMMEDIATE`` (database-level write
lock, acquired up front, waits up to ``busy_timeout``). That serialises all
writers, which is correct but not scalable — SQLite is for development and
tests only.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _record):  # pragma: no cover - driver hook
            # Let SQLAlchemy (not the driver) decide when transactions begin.
            dbapi_conn.isolation_level = None
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver hook
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(database_url, pool_pre_ping=True)


class Database:
    """Owns the engine and a session factory."""

    def __init__(self, database_url: str):
        self.url = database_url
        self.engine = make_engine(database_url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Additive schema upgrade for databases created by an earlier iteration.

        ``create_all`` creates missing *tables* but never alters existing ones.
        Columns added since iteration 01 are listed here and added with a safe
        default. Note ``users.email_verified`` defaults to FALSE: existing
        accounts (including existing platform operators) must verify their
        e-mail (or an operator runs ``verify-email``) before they can spend or
        use ``/admin``. A real migration tool (Alembic) is still a to-do.
        """
        # One connection for inspection and DDL: on SQLite every transaction
        # takes the write lock, so a second connection would wait on the first.
        with self.engine.begin() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            for table, column, ddl in ADDED_COLUMNS:
                if table not in tables:
                    continue
                existing = {c["name"] for c in inspector.get_columns(table)}
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """A session whose work is committed on success and rolled back on error."""
        session = self.sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


# (table, column, DDL) — columns added after iteration 01, see Database._add_missing_columns.
ADDED_COLUMNS = (
    ("users", "email_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "email_verified_at", "FLOAT"),
    ("email_verification_tokens", "purpose", "VARCHAR(10) NOT NULL DEFAULT 'verify'"),
    ("usage_records", "hold_tokens", "INTEGER"),
    ("gateway_sessions", "overage_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("gateway_sessions", "overage_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("robots", "conformance", "JSON"),
)


def advisory_key(name: str) -> int:
    """Stable signed 64-bit key for ``pg_advisory_xact_lock``."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def advisory_lock(session: Session, *names: str) -> None:
    """Serialise check-then-insert sequences that have no row to lock (throttling counters).

    PostgreSQL: ``pg_advisory_xact_lock`` per name, taken in sorted order (so
    two transactions can never wait on each other in a cycle) and released at
    commit/rollback. SQLite: no-op, because every SQLite transaction already
    starts with ``BEGIN IMMEDIATE`` (database-wide write lock).
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    for name in sorted(set(names)):
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(name)})


def lock_wallet(session: Session, org_id: str):
    """Lock (and lazily create) the org's wallet row for the rest of the transaction."""
    from .models import Wallet

    wallet = session.execute(select(Wallet).where(Wallet.org_id == org_id).with_for_update()).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(org_id=org_id)
        session.add(wallet)
        session.flush()
        wallet = session.execute(select(Wallet).where(Wallet.org_id == org_id).with_for_update()).scalar_one()
    return wallet
