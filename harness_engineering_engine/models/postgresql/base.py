# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy base metadata and shared helpers.

This module is only imported when ``DB_BACKEND=postgresql``.
DynamoDB-only installs never import SQLAlchemy.

The ``Base.table_prefix`` is set by ``Config._initialize_db_session`` from the
``pg_table_prefix`` setting (e.g. ``"hsk_"``).  All models use
``declared_attr`` for ``__tablename__`` so the prefix is applied when the
class is defined.
"""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

try:
    from sqlalchemy.ext.declarative import declarative_base
except ImportError:  # pragma: no cover - DynamoDB-only environments
    raise ImportError(
        "SQLAlchemy is required for PostgreSQL backend. "
        "Install with: pip install harness-engineering-engine[postgresql]"
    )

Base = declarative_base()
Base.table_prefix = ""  # type: ignore[attr-defined]


def prefixed_table(name: str) -> str:
    """Return ``name`` with the configured table prefix prepended."""
    return f"{Base.table_prefix}{name}"


def prefixed_index(name: str) -> str:
    """Return an index name with the configured table prefix prepended."""
    return f"{Base.table_prefix}{name}"


def normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    """Convert a SQLAlchemy model instance to a normalized dict."""
    if row is None:
        return None

    from ...utils.normalization import normalize_to_json

    if isinstance(row, dict):
        return normalize_to_json(row)

    if hasattr(row, "__table__"):
        result = {}
        for col in row.__table__.columns:
            key = col.name
            val = getattr(row, key, None)
            result[key] = _serialize_value(val)
        return normalize_to_json(result)

    return normalize_to_json(row)


def _serialize_value(val: Any) -> Any:
    """Serialize individual SQLAlchemy column values to JSON-safe types."""
    import datetime
    from decimal import Decimal
    from uuid import UUID as UUIDType

    if val is None:
        return None
    if isinstance(val, UUIDType):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        # Keep as-is — graphene DateTime scalar handles .isoformat()
        return val
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (list, dict)):
        return val
    return val


__all__ = ["Base", "normalize_row", "prefixed_table", "prefixed_index"]
