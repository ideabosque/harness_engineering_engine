# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for CliPackage entity.

Mirrors the DynamoDB CliPackageModel schema with PostgreSQL-appropriate types.
"""
from __future__ import print_function

__author__ = "bibow"

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    String,
    Text,
    TIMESTAMP,
    text,
)
from sqlalchemy.orm import declared_attr
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, prefixed_index, prefixed_table


class CliPackageModel(Base):
    """SQLAlchemy model for the CLI package entity (table: cli_packages)."""

    @declared_attr
    def __tablename__(cls) -> str:
        return prefixed_table("cli_packages")

    # Primary key: composite (partition_key, cli_package_uuid)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    cli_package_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    # Tenant metadata
    endpoint_id = Column(String(64))
    part_id = Column(String(64))

    # Package identity
    package_name = Column(String(255), nullable=False)
    github_repository_url = Column(String(512), nullable=False)
    version = Column(String(64), nullable=False)
    git_ref = Column(String(128))
    description = Column(Text)

    # Lifecycle
    status = Column(String(32), nullable=False, default="active")
    enabled = Column(Boolean, nullable=False, default=True)

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
        Index(prefixed_index("idx_cli_packages_partition_name"), "partition_key", "package_name"),
        Index(prefixed_index("idx_cli_packages_partition_updated_at"), "partition_key", "updated_at"),
    )


__all__ = ["CliPackageModel"]