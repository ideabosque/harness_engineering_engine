# -*- coding: utf-8 -*-
"""Alembic migration environment for harness_engineering_engine."""
from __future__ import print_function

import logging
from logging.config import fileConfig

from alembic import context

from harness_engineering_engine.models.postgresql.base import Base

import os as _os

_pg_table_prefix = _os.environ.get("PG_TABLE_PREFIX", "")
if not _pg_table_prefix:
    try:
        from harness_engineering_engine.handlers.config import Config

        if Config._initialized:
            _pg_table_prefix = Config.PG_TABLE_PREFIX
    except Exception:
        pass
Base.table_prefix = _pg_table_prefix or ""

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

logger = logging.getLogger("alembic.env")


def get_url():
    import os

    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url

    try:
        from harness_engineering_engine.handlers.config import Config

        setting = Config.get_setting()
        from urllib.parse import quote_plus

        password = quote_plus(setting["db_password"])
        return (
            f"postgresql+psycopg2://{setting['db_user']}:{password}"
            f"@{setting['db_host']}:{setting['db_port']}/{setting['db_schema']}"
        )
    except Exception:
        pass

    return "postgresql://user:pass@localhost/dbname"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        context.config.get_section(context.config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=get_url(),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
