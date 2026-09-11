# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for CommandRun entity.

Distributed-safe backing store for the async command run registry — see the
DynamoDB sibling in ``models/dynamodb/command_run.py`` and
``docs/DEVELOPMENT_PLAN.md`` §18 G-6.
"""

from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declared_attr

from .base import Base, prefixed_index, prefixed_table


class CommandRunModel(Base):
    """SQLAlchemy model for an async background command run (table: command_runs)."""

    @declared_attr
    def __tablename__(cls) -> str:
        return prefixed_table("command_runs")

    # Primary key: composite (partition_key, run_uuid)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    run_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    # Tenant metadata
    endpoint_id = Column(String(64))
    part_id = Column(String(64))

    # Run identity
    skill_name = Column(String(255), nullable=False)
    argv = Column(Text, nullable=False)  # JSON-encoded list[str]

    # Status
    status = Column(String(32), nullable=False, default="running")
    exit_code = Column(String(16))
    stdout = Column(Text)
    stderr = Column(Text)
    timed_out = Column(Boolean, nullable=False, default=False)
    truncated = Column(Boolean, nullable=False, default=False)
    output_truncated_bytes = Column(Integer, nullable=False, default=0)

    # Timing
    started_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    completed_at = Column(TIMESTAMP(timezone=True))

    # Bookkeeping
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    updated_by = Column(String(64), nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        server_onupdate=text("NOW()"),
    )

    __table_args__ = (
        Index(
            prefixed_index("idx_command_runs_partition_completed_at"),
            "partition_key",
            "completed_at",
        ),
    )


__all__ = ["CommandRunModel"]
