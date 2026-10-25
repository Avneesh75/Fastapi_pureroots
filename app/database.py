"""Database engine, session factory and the two declarative bases.

* ``Base`` — tables OWNED by FastAPI (created by Alembic).
* ``CatalogBase`` — READ-ONLY mappings of Django-owned tables. Alembic never
  sees this metadata; tests create these tables with ``CatalogBase.metadata.create_all``.
"""
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for FastAPI-owned tables (managed by Alembic)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CatalogBase(DeclarativeBase):
    """Declarative base for read-only Django tables. Never migrated from here."""

    metadata = MetaData()


def build_engine(url: str) -> Engine:
    """Create an engine; in-memory SQLite (tests) shares one connection via StaticPool."""
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url.rstrip("/").endswith(":"):
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
