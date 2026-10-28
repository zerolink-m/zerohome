"""push/pushmeta on users, notify on cams, invitations + sharings tables, drop allowedusers

Revision ID: 0006_push_notify_invitations_sharings
Revises: 0005_cam_recording
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_push_notify_invitations_sharings"
down_revision = "0005_cam_recording"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("push", sa.Enum("telegram", name="user_push"), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("pushmeta", sa.JSON(), nullable=True),
    )

    op.add_column(
        "cams",
        sa.Column("notify", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.drop_column("cams", "allowedusers")

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cam_id", sa.Integer(), sa.ForeignKey("cams.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("invited_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "accepted", name="invitation_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("invited_at", sa.BigInteger(), nullable=False),
        sa.Column("responded_at", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "sharings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uid", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.Enum("record", "stream", name="sharing_type"), nullable=False),
        sa.Column("cid", sa.Integer(), sa.ForeignKey("cams.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sharings")
    op.drop_table("invitations")

    op.add_column(
        "cams",
        sa.Column("allowedusers", sa.String(255), nullable=False, server_default=""),
    )
    op.drop_column("cams", "notify")

    op.drop_column("users", "pushmeta")
    op.drop_column("users", "push")
