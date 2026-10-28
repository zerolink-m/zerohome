"""add allowrecorde column to users

Revision ID: 0004_user_allowrecorde
Revises: 0003_cam_name
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_user_allowrecorde"
down_revision = "0003_cam_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("allowrecorde", sa.SmallInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "allowrecorde")
