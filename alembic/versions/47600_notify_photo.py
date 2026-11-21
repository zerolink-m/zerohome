"""Notification queue and motion photo object key.

Revision ID: 47600_notify_photo
Revises: 47500_cluster_leases
"""
from alembic import op
import sqlalchemy as sa

revision = "47600_notify_photo"
down_revision = "47500_cluster_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("device_type", sa.String(16), nullable=True),
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("photo_key", sa.String(255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.BigInteger(), nullable=False),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_generation", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_notification_jobs_state_available",
        "notification_jobs",
        ["state", "available_at"],
    )
    # photo_filename stays: rows written before this migration are served from it.
    op.add_column("motion_events", sa.Column("photo_key", sa.String(255), nullable=True))
    op.add_column("motion_events", sa.Column("photo_state", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("motion_events", "photo_state")
    op.drop_column("motion_events", "photo_key")
    op.drop_index("ix_notification_jobs_state_available", table_name="notification_jobs")
    op.drop_table("notification_jobs")
