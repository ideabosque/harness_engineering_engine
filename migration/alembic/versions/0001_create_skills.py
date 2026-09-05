"""Create skills table

Revision ID: 0001
Revises:
Create Date: 2026-08-25

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from harness_engineering_engine.models.postgresql.base import prefixed_table, prefixed_index

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")

    op.create_table(
        prefixed_table("skills"),
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column(
            "skill_uuid",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("endpoint_id", sa.String(64)),
        sa.Column("part_id", sa.String(64)),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("git_repository_url", sa.Text),
        sa.Column("s3_bucket", sa.String(255)),
        sa.Column("s3_key", sa.Text),
        sa.Column("s3_version_id", sa.String(255)),
        sa.Column("artifact_checksum", sa.String(64)),
        sa.Column("content_checksum", sa.String(64)),
        sa.Column("local_path", sa.Text),
        sa.Column("deployment_status", sa.String(32), nullable=False, default="uploaded"),
        sa.Column("enabled", sa.Boolean, nullable=False, default=True),
        sa.Column("is_active", sa.Boolean, nullable=False, default=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("partition_key", "skill_uuid"),
    )

    op.create_index(
        prefixed_index("idx_skills_partition_name"),
        prefixed_table("skills"),
        ["partition_key", "name"],
    )
    op.create_index(
        prefixed_index("idx_skills_partition_updated_at"),
        prefixed_table("skills"),
        ["partition_key", "updated_at"],
    )
    op.create_index(
        prefixed_index("idx_skills_partition_active"),
        prefixed_table("skills"),
        ["partition_key", "is_active", "enabled"],
    )


def downgrade():
    op.drop_index(prefixed_index("idx_skills_partition_active"), table_name=prefixed_table("skills"))
    op.drop_index(prefixed_index("idx_skills_partition_updated_at"), table_name=prefixed_table("skills"))
    op.drop_index(prefixed_index("idx_skills_partition_name"), table_name=prefixed_table("skills"))
    op.drop_table(prefixed_table("skills"))
