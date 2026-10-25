"""Alembic environment for FastAPI-owned tables ONLY.

* ``target_metadata`` is ``Base.metadata`` — never ``CatalogBase`` (Django owns those tables).
* ``include_object`` ignores every table not defined on ``Base`` so autogenerate never
  proposes dropping Django's tables that live in the same database.
* The URL comes from ``DATABASE_URL`` via ``app.config``.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app import models as _models  # noqa: F401  (registers owned models on Base.metadata)
from app.config import get_settings
from app.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
OWNED_TABLES = frozenset(target_metadata.tables)


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table":
        return name in OWNED_TABLES
    table = getattr(obj, "table", None)
    table_name = getattr(table, "name", None)
    if table_name is not None and table_name not in OWNED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
