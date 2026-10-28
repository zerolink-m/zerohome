"""add name column to cams

Revision ID: 0003_cam_name
Revises: 0002_cam_path
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_cam_name"
down_revision = "0002_cam_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cams",
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("cams", "name")
