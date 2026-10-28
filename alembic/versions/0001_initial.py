"""initial schema: users, sessions, cams

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("surname", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("admin", "user", name="user_role"),
            nullable=False,
            server_default="user",
        ),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
        mysql_engine="InnoDB",
    )
    op.create_unique_constraint("uq_users_email", "users", ["email"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uid", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ua", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_sessions_uid", "sessions", ["uid"])

    op.create_table(
        "cams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ownuser", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("allowedusers", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("proto", sa.Enum("rtsp", name="cam_proto"), nullable=False),
        sa.Column("addr", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column(
            "controlproto",
            sa.Enum("onvif", name="cam_control_proto"),
            nullable=True,
        ),
        sa.Column("controladdr", sa.String(length=255), nullable=True),
        sa.Column("controlport", sa.Integer(), nullable=True),
        sa.Column("controlusername", sa.String(length=255), nullable=True),
        sa.Column("controlpassword", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_cams_ownuser", "cams", ["ownuser"])


def downgrade() -> None:
    op.drop_table("cams")
    op.drop_table("sessions")
    op.drop_table("users")
