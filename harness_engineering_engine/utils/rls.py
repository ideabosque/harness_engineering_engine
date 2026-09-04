# -*- coding: utf-8 -*-
"""Row-Level Security (RLS) helpers for the PostgreSQL backend.

Enforces tenant isolation at the database level so that even if a query
forgets to filter on ``partition_key``, PostgreSQL still restricts rows to the
current tenant context. Only imported when ``DB_BACKEND=postgresql``.

- ``set_rls_context(session, partition_key)`` sets ``SET app.tenant_id`` at the
  start of each request/entry point.
- ``create_rls_policies(engine)`` applies RLS policies to the skills table.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

# All harness-engineering PostgreSQL tables carry a ``partition_key`` column.
_RLS_TABLES = [
    "skills",
    "cli_packages",
]


def set_rls_context(session: Any, partition_key: str) -> None:
    """Set the RLS tenant context for the current database session.

    Uses connection-level ``SET`` (not ``SET LOCAL``) so the tenant context
    survives the ``commit()`` a mutation issues before its response resolvers
    read related rows. The scoped session is removed at the request boundary,
    and every entry point re-sets the context before its first query.
    """
    if not partition_key:
        raise ValueError("partition_key must be a non-empty string for RLS context.")

    session.execute(
        text("SET app.tenant_id = :tenant"),
        {"tenant": partition_key},
    )


def create_rls_policies(engine: Any) -> None:
    """Enable RLS and create tenant-isolation policies on all skill tables.

    Idempotent: existing policies are dropped before re-creation.
    """
    from ..models.postgresql.base import prefixed_table

    with engine.connect() as conn:
        for table_name in _RLS_TABLES:
            actual_name = prefixed_table(table_name)
            try:
                conn.execute(
                    text(f"ALTER TABLE {actual_name} ENABLE ROW LEVEL SECURITY")
                )
                conn.execute(
                    text(f"ALTER TABLE {actual_name} FORCE ROW LEVEL SECURITY")
                )
                conn.execute(
                    text(f"DROP POLICY IF EXISTS tenant_isolation ON {actual_name}")
                )
                conn.execute(
                    text(
                        f"CREATE POLICY tenant_isolation ON {actual_name} "
                        f"USING (partition_key = current_setting('app.tenant_id', true))"
                    )
                )
                logger.debug(f"RLS policy applied to {actual_name}")
            except Exception as exc:
                logger.warning(f"Failed to apply RLS to {actual_name}: {exc}")
        conn.commit()


__all__ = ["set_rls_context", "create_rls_policies", "_RLS_TABLES"]
