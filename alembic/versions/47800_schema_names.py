"""Rename legacy columns to the names the models map.

Revision ID: 47800_schema_names
Revises: 47700_lease_holder

A clean chain never renamed cams/users (and 42051 renamed switches and
invitations the other way). A database that already has the new names is
left untouched: each column is renamed only when the old name is present
and the new name is not.

sessions.last_login is added when missing. sessions.is_pwa is not dropped.
"""
import re

from alembic import op
import sqlalchemy as sa

revision = "47800_schema_names"
down_revision = "47700_lease_holder"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_switches_added_by_consistency"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# (old, new, column definition). Definitions match the live types.
# switches.control_proto keeps the utf8mb4_unicode_ci collation 42051 set.
# controlmeta and invitations.updated_at are relaxed to NULL: the models
# are nullable, and creating an invitation does not set responded_at.
_CAM_RENAMES = (
    ("ownuser", "owner", "INT NOT NULL"),
    ("proto", "protocol", "ENUM('rtsp') NOT NULL"),
    ("addr", "address", "VARCHAR(255) NOT NULL"),
    ("controlproto", "control_protocol", "ENUM('onvif') NULL"),
    ("controladdr", "control_address", "VARCHAR(255) NULL"),
    ("controlport", "control_port", "INT NULL"),
    ("controlusername", "control_username", "VARCHAR(255) NULL"),
    ("controlpassword", "control_password", "VARCHAR(255) NULL"),
    ("recorde", "record", "SMALLINT NOT NULL DEFAULT 0"),
    ("recordetype", "record_type", "ENUM('local') NULL"),
    ("recordemeta", "record_metadata", "JSON NULL"),
)
_SWITCH_RENAMES = (
    ("ownuser", "owner", "INT NOT NULL"),
    (
        "control_proto",
        "control_protocol",
        "ENUM('tuya_local', 'tuya_cloud', 'custom') COLLATE utf8mb4_unicode_ci NOT NULL",
    ),
    ("controlmeta", "control_metadata", "JSON NULL"),
    ("is_master", "is_main", "SMALLINT NOT NULL DEFAULT 0"),
)
_USER_RENAMES = (
    ("allowrecord", "allow_record", "SMALLINT NOT NULL DEFAULT 0"),
    ("allowtuyalocal", "allow_tuya_local", "SMALLINT NOT NULL DEFAULT 0"),
    ("pushmeta", "push_metadata", "JSON NULL"),
)
_INVITATION_RENAMES = (
    ("added_at", "invited_at", "BIGINT NOT NULL"),
    ("updated_at", "responded_at", "BIGINT NULL"),
)

# Reverse renames keep the type this revision wrote. Nullability that was
# relaxed stays relaxed: pending invitations and switch metadata may be NULL,
# so downgrade does not tighten those columns back to NOT NULL.
_CAM_REVERTS = tuple((new, old, ddl) for old, new, ddl in _CAM_RENAMES)
_SWITCH_REVERTS = tuple((new, old, ddl) for old, new, ddl in _SWITCH_RENAMES)
_USER_REVERTS = tuple((new, old, ddl) for old, new, ddl in _USER_RENAMES)
_INVITATION_REVERTS = tuple((new, old, ddl) for old, new, ddl in _INVITATION_RENAMES)

# Text of the check in 42051's downgrade (is_main) and upgrade (is_master).
_CHECK_IS_MAIN = """
ALTER TABLE switches ADD CONSTRAINT ck_switches_added_by_consistency CHECK ((
  (added_by = 'user' AND connection_id IS NULL AND external_id IS NULL
   AND external_switch_channel IS NULL AND is_main = 0 AND parent IS NULL)
  OR
  (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL
   AND ((is_main = 1 AND parent IS NULL AND external_switch_channel IS NULL)
        OR (is_main = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)))
))
"""
_CHECK_IS_MASTER = """
ALTER TABLE switches ADD CONSTRAINT ck_switches_added_by_consistency CHECK ((
  (added_by = 'user' AND connection_id IS NULL AND external_id IS NULL
   AND external_switch_channel IS NULL AND is_master = 0 AND parent IS NULL)
  OR
  (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL
   AND ((is_master = 1 AND parent IS NULL AND external_switch_channel IS NULL)
        OR (is_master = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)))
))
"""


def rename_if_legacy(columns: set[str], old: str, new: str) -> str | None:
    """Return ``new`` when ``old`` is present and ``new`` is not, else None."""
    if old in columns and new not in columns:
        return new
    return None


def planned_renames(
    columns: set[str], specs: tuple[tuple[str, str, str], ...]
) -> list[tuple[str, str, str]]:
    return [spec for spec in specs if rename_if_legacy(columns, spec[0], spec[1])]


def _column_names(table: str) -> set[str]:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
        ),
        {"table_name": table},
    )
    return {str(row[0]) for row in rows}


def _constraint_exists(table: str, name: str) -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
            "AND CONSTRAINT_NAME = :constraint_name LIMIT 1"
        ),
        {"table_name": table, "constraint_name": name},
    ).first()
    return row is not None


def _change_column(table: str, old: str, new: str, column_def: str) -> None:
    # Names come from the constants above, never from outside input.
    for ident in (table, old, new):
        if _IDENT.fullmatch(ident) is None:
            raise RuntimeError(f"refusing identifier {ident!r}")
    if ";" in column_def:
        raise RuntimeError("refusing column definition")
    op.execute(f"ALTER TABLE {table} CHANGE COLUMN {old} {new} {column_def}")


def _apply(table: str, columns: set[str], specs: tuple[tuple[str, str, str], ...]) -> None:
    for old, new, column_def in planned_renames(columns, specs):
        _change_column(table, old, new, column_def)


def upgrade() -> None:
    switches = _column_names("switches")
    # The check references is_master. Drop it before that rename, then recreate
    # it on is_main (same text as the downgrade of 42051fbfaab8).
    renaming_master = rename_if_legacy(switches, "is_master", "is_main") is not None
    if renaming_master and _constraint_exists("switches", _CHECK_NAME):
        op.drop_constraint(_CHECK_NAME, "switches", type_="check")

    _apply("cams", _column_names("cams"), _CAM_RENAMES)
    _apply("switches", switches, _SWITCH_RENAMES)
    _apply("users", _column_names("users"), _USER_RENAMES)
    _apply("invitations", _column_names("invitations"), _INVITATION_RENAMES)

    if renaming_master and not _constraint_exists("switches", _CHECK_NAME):
        op.execute(_CHECK_IS_MAIN)

    # is_pwa is intentionally left in place.
    sessions = _column_names("sessions")
    if "last_login" not in sessions:
        op.execute("ALTER TABLE sessions ADD COLUMN last_login BIGINT NULL")
        op.execute("UPDATE sessions SET last_login = added_at")
        op.execute("ALTER TABLE sessions MODIFY last_login BIGINT NOT NULL")


def downgrade() -> None:
    switches = _column_names("switches")
    # Restore the check onto is_master, same text as the upgrade of 42051.
    # last_login is not dropped: it may have existed before this revision.
    renaming_main = rename_if_legacy(switches, "is_main", "is_master") is not None
    if renaming_main and _constraint_exists("switches", _CHECK_NAME):
        op.drop_constraint(_CHECK_NAME, "switches", type_="check")

    _apply("invitations", _column_names("invitations"), _INVITATION_REVERTS)
    _apply("users", _column_names("users"), _USER_REVERTS)
    _apply("switches", switches, _SWITCH_REVERTS)
    _apply("cams", _column_names("cams"), _CAM_REVERTS)

    if renaming_main and not _constraint_exists("switches", _CHECK_NAME):
        op.execute(_CHECK_IS_MASTER)
