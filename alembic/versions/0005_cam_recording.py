"""add recording columns to cams

Revision ID: 0005_cam_recording
Revises: 0004_user_allowrecorde
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_cam_recording"
down_revision = "0004_user_allowrecorde"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cams",
        sa.Column("recorde", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "cams",
        sa.Column("recordetype", sa.Enum("local", name="cam_record_type"), nullable=True),
    )
    op.add_column(
        "cams",
        sa.Column("recordemeta", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cams", "recordemeta")
    op.drop_column("cams", "recordetype")
    op.drop_column("cams", "recorde")
