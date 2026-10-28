"""Add 'device_use' to sharing_type enum

Revision ID: 0010_sharing_type_device_use
Revises: 0009_switches_and_polymorphic_sharing
Create Date: 2026-08-08

MySQL stores enums inline on the column (there's no separate CREATE TYPE
like Postgres). To extend the enum we `ALTER TABLE ... MODIFY COLUMN` with
the full new value list.

Downgrade: shrinking the enum would leave existing 'device_use' rows
invalid, so we DELETE them first (data loss by design — the value doesn't
exist in the old schema).
"""

from alembic import op

revision = "0010_sharing_type_device_use"
down_revision = "0009_switches_and_polymorphic_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sharings "
        "MODIFY COLUMN `type` ENUM('record', 'stream', 'device_use') NOT NULL"
    )


def downgrade() -> None:
    op.execute("DELETE FROM sharings WHERE `type` = 'device_use'")
    op.execute(
        "ALTER TABLE sharings "
        "MODIFY COLUMN `type` ENUM('record', 'stream') NOT NULL"
    )
