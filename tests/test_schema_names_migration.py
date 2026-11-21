"""The rename helper decides from a column set. It does not connect anywhere."""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "47800_schema_names.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("rev_47800_schema_names", _PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_renames_when_old_present_and_new_absent():
    migration = _load()
    columns = {"ownuser", "proto", "is_master", "controlmeta", "allowrecord", "added_at"}
    assert migration.rename_if_legacy(columns, "ownuser", "owner") == "owner"
    assert migration.planned_renames(
        columns,
        (
            ("ownuser", "owner", "INT NOT NULL"),
            ("proto", "protocol", "ENUM('rtsp') NOT NULL"),
            ("is_master", "is_main", "SMALLINT NOT NULL DEFAULT 0"),
        ),
    ) == [
        ("ownuser", "owner", "INT NOT NULL"),
        ("proto", "protocol", "ENUM('rtsp') NOT NULL"),
        ("is_master", "is_main", "SMALLINT NOT NULL DEFAULT 0"),
    ]


def test_skips_when_new_name_already_exists():
    migration = _load()
    # The new name wins even if a leftover old column is still there.
    columns = {"owner", "protocol", "is_main", "ownuser", "proto"}
    assert migration.rename_if_legacy(columns, "ownuser", "owner") is None
    assert migration.rename_if_legacy(columns, "proto", "protocol") is None
    assert migration.planned_renames(
        columns,
        (
            ("ownuser", "owner", "INT NOT NULL"),
            ("proto", "protocol", "ENUM('rtsp') NOT NULL"),
        ),
    ) == []


def test_skips_when_old_name_is_absent():
    migration = _load()
    columns = {"name", "enabled", "port"}
    assert migration.rename_if_legacy(columns, "ownuser", "owner") is None
    assert migration.rename_if_legacy(set(), "controlmeta", "control_metadata") is None
    assert migration.planned_renames(
        columns, (("ownuser", "owner", "INT NOT NULL"),)
    ) == []
