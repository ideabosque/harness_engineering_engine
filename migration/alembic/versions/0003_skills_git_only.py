"""Drop S3 artifact columns from skills, add git pin columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-04

Skills are now sourced directly from git with no S3 artifact store in
between. ``s3_bucket``/``s3_key``/``s3_version_id`` and ``artifact_checksum``
are replaced by ``git_ref`` (the branch/tag requested at deploy time) and
``resolved_commit`` (the commit SHA it resolved to).
"""
from alembic import op
import sqlalchemy as sa
from harness_engineering_engine.models.postgresql.base import prefixed_table

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    table = prefixed_table("skills")
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_column("s3_bucket")
        batch_op.drop_column("s3_key")
        batch_op.drop_column("s3_version_id")
        batch_op.drop_column("artifact_checksum")
        batch_op.add_column(sa.Column("git_ref", sa.String(255)))
        batch_op.add_column(sa.Column("resolved_commit", sa.String(64)))


def downgrade():
    table = prefixed_table("skills")
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_column("resolved_commit")
        batch_op.drop_column("git_ref")
        batch_op.add_column(sa.Column("artifact_checksum", sa.String(64)))
        batch_op.add_column(sa.Column("s3_version_id", sa.String(255)))
        batch_op.add_column(sa.Column("s3_key", sa.Text()))
        batch_op.add_column(sa.Column("s3_bucket", sa.String(255)))
