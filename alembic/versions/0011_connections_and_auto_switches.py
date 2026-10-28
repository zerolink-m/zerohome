"""connections table + switches.added_by/connection_id/external_id + integrity

Revision ID: 0011_connections_and_auto_switches
Revises: 0010_sharing_type_device_use
Create Date: 2026-08-08

Adds the `connections` table (per-user Tuya-cloud etc. credentials) and three
new columns on `switches` that link a switch back to the connection that
auto-synced it. A CHECK constraint enforces the invariant:

    added_by='user'  ↔  connection_id IS NULL AND external_id IS NULL
    added_by='auto'  ↔  connection_id IS NOT NULL AND external_id IS NOT NULL

Downgrade drops everything in reverse. Rows with added_by='auto' cannot exist
in the pre-0011 schema (no such column), so downgrade also drops those switches
— data loss by design, since the connections that owned them are being dropped
too.

MySQL note: enums are inline on the column (no separate CREATE TYPE step like
Postgres). Follows the same shape as 0001/0005 in this repo.
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_connections_and_auto_switches"
down_revision = "0010_sharing_type_device_use"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) connections table
    op.create_table(
        "connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uid", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("tuya_cloud", name="connection_provider"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("last_synced_at", sa.BigInteger(), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_connections_uid", "connections", ["uid"])

    # 2) switches: new columns
    op.add_column(
        "switches",
        sa.Column(
            "added_by",
            sa.Enum("user", "auto", name="switch_added_by"),
            nullable=False,
            server_default="user",
        ),
    )
    op.add_column(
        "switches",
        sa.Column(
            "connection_id",
            sa.Integer(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("switches", sa.Column("external_id", sa.String(255), nullable=True))

    # 3) constraints
    op.create_check_constraint(
        "ck_switches_added_by_consistency",
        "switches",
        "(added_by = 'user' AND connection_id IS NULL AND external_id IS NULL) "
        "OR (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_switches_connection_external",
        "switches",
        ["connection_id", "external_id"],
    )
    op.create_index("ix_switches_connection_id", "switches", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_switches_connection_id", table_name="switches")
    op.drop_constraint("uq_switches_connection_external", "switches", type_="unique")
    op.drop_constraint("ck_switches_added_by_consistency", "switches", type_="check")

    # Any auto-synced switch is meaningless without its columns/connection.
    op.execute("DELETE FROM switches WHERE added_by = 'auto'")

    op.drop_column("switches", "external_id")
    op.drop_column("switches", "connection_id")
    op.drop_column("switches", "added_by")

    op.drop_index("ix_connections_uid", table_name="connections")
    op.drop_table("connections")
