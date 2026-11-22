"""Allow NULL for optional user settings, matching the User model.

Revision ID: 48000_user_nullable_settings
Revises: 47900_runtime_configuration
"""
from alembic import op
import sqlalchemy as sa

revision = "48000_user_nullable_settings"
down_revision = "47900_runtime_configuration"
branch_labels = None
depends_on = None

_COLUMNS = ("allow_record", "allow_tuya_local")


def _column_info() -> dict[str, dict]:
    columns = {item["name"]: item for item in sa.inspect(op.get_bind()).get_columns("users")}
    missing = set(_COLUMNS) - columns.keys()
    if missing:
        raise RuntimeError(f"users table is missing expected columns: {', '.join(sorted(missing))}")
    return columns


def upgrade() -> None:
    columns = _column_info()
    for name in _COLUMNS:
        if not columns[name]["nullable"]:
            op.alter_column(
                "users",
                name,
                existing_type=sa.SmallInteger(),
                existing_nullable=False,
                nullable=True,
            )


def downgrade() -> None:
    columns = _column_info()
    for name in _COLUMNS:
        if columns[name]["nullable"]:
            # The previous schema supplied 0 as the default. Preserve existing
            # rows by replacing NULL with that same legacy value before tightening.
            op.execute(f"UPDATE users SET {name} = 0 WHERE {name} IS NULL")
            op.alter_column(
                "users",
                name,
                existing_type=sa.SmallInteger(),
                existing_nullable=True,
                nullable=False,
            )
