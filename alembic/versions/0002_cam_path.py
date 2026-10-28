"""add path column to cams

Revision ID: 0002_cam_path
Revises: 0001_initial
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_cam_path"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cams",
        sa.Column("path", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("cams", "path")
