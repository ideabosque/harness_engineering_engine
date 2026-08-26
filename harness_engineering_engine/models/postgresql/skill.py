# -*- coding: utf-8 -*-
"""PostgreSQL SQLAlchemy model for Skill entity.

Mirrors the DynamoDB SkillModel schema with PostgreSQL-appropriate types.
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


class SkillModel(Base):
    """SQLAlchemy model for the Skill entity (table: skills)."""

    @declared_attr
    def __tablename__(cls) -> str:
        return prefixed_table("skills")

    # Primary key: composite (partition_key, skill_uuid)
    partition_key = Column(String(128), nullable=False, primary_key=True)
    skill_uuid = Column(
        UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    # Tenant metadata
    endpoint_id = Column(String(64))
    part_id = Column(String(64))

    # Public identity
    name = Column(String(255), nullable=False)
    version = Column(String(64), nullable=False)
    description = Column(Text)

    # Deployment source
    source_type = Column(String(16))
    source_ref = Column(Text)

    # S3 artifact location
    s3_bucket = Column(String(255))
    s3_key = Column(Text)
    s3_version_id = Column(String(255))

    # Integrity
    artifact_checksum = Column(String(64))
    content_checksum = Column(String(64))

    # Runtime cache
    local_path = Column(Text)

    # Lifecycle
    deployment_status = Column(String(32), nullable=False, default="uploaded")
    enabled = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=False)

    # Bookkeeping
    registered_at = Column(TIMESTAMP(timezone=True))
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
        Index(prefixed_index("idx_skills_partition_name"), "partition_key", "name"),
        Index(prefixed_index("idx_skills_partition_updated_at"), "partition_key", "updated_at"),
        Index(prefixed_index("idx_skills_partition_active"), "partition_key", "is_active", "enabled"),
    )


__all__ = ["SkillModel"]
