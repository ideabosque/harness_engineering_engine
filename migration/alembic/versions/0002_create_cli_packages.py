"""Create cli_packages table

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from harness_engineering_engine.models.postgresql.base import prefixed_table, prefixed_index

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        prefixed_table("cli_packages"),
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column(
            "cli_package_uuid",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("endpoint_id", sa.String(64)),
        sa.Column("part_id", sa.String(64)),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("git_repository_url", sa.String(512), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("git_ref", sa.String(128)),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(32), nullable=False, default="active"),
        sa.Column("enabled", sa.Boolean, nullable=False, default=True),
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
        sa.PrimaryKeyConstraint("partition_key", "cli_package_uuid"),
    )

    op.create_index(
        prefixed_index("idx_cli_packages_partition_name"),
        prefixed_table("cli_packages"),
        ["partition_key", "package_name"],
    )
    op.create_index(
        prefixed_index("idx_cli_packages_partition_updated_at"),
        prefixed_table("cli_packages"),
        ["partition_key", "updated_at"],
    )

    # Enable RLS on the new table
    op.execute(
        f"ALTER TABLE {prefixed_table('cli_packages')} ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {prefixed_table('cli_packages')} FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"DROP POLICY IF EXISTS tenant_isolation ON {prefixed_table('cli_packages')}"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation ON {prefixed_table('cli_packages')} "
        f"USING (partition_key = current_setting('app.tenant_id', true))"
    )


def downgrade():
    op.drop_index(prefixed_index("idx_cli_packages_partition_updated_at"), table_name=prefixed_table("cli_packages"))
    op.drop_index(prefixed_index("idx_cli_packages_partition_name"), table_name=prefixed_table("cli_packages"))
    op.drop_table(prefixed_table("cli_packages"))