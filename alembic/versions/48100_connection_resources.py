"""Add provider-neutral connection credentials and resources.

Revision ID: 48100_connection_resources
Revises: 48000_user_nullable_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "48100_connection_resources"
down_revision = "48000_user_nullable_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("connections", "provider", existing_type=sa.Enum("tuya_cloud", name="connection_provider"), type_=sa.String(64), existing_nullable=False)
    op.add_column("connections", sa.Column("status", sa.String(32), nullable=False, server_default="unknown"))
    op.add_column("connections", sa.Column("last_checked_at", sa.BigInteger(), nullable=True))
    op.create_table(
        "connection_credentials",
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("credentials", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "connection_resources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("available", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("connection_id", "resource_type", "external_id", name="uq_connection_resource"),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM connections WHERE provider != 'tuya_cloud' OR provider IS NULL")).scalar_one():
        raise RuntimeError("Remove non-Tuya connections before downgrading")
    op.drop_table("connection_resources")
    op.drop_table("connection_credentials")
    op.drop_column("connections", "last_checked_at")
    op.drop_column("connections", "status")
    op.alter_column("connections", "provider", existing_type=sa.String(64), type_=sa.Enum("tuya_cloud", name="connection_provider"), existing_nullable=False)
