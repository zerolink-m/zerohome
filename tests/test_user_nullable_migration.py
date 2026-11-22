from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "48000_user_nullable_settings.py"
)


def test_revision_follows_runtime_configuration_and_is_the_head():
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "alembic"),
    )
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision("48000_user_nullable_settings")

    assert revision.down_revision == "47900_runtime_configuration"
    assert script.get_heads() == ["48000_user_nullable_settings"]


def _load_migration():
    spec = spec_from_file_location("rev_48000_user_nullable_settings", MIGRATION_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Inspector:
    def __init__(self, nullable_by_name):
        self.nullable_by_name = nullable_by_name

    def get_columns(self, table):
        assert table == "users"
        return [
            {"name": name, "nullable": nullable}
            for name, nullable in self.nullable_by_name.items()
        ]


def _run_upgrade(nullable_by_name, monkeypatch):
    migration = _load_migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect=mysql.dialect(),
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _Inspector(nullable_by_name),
    )
    migration.op.get_bind = lambda: object()
    migration.upgrade()
    return output.getvalue()


def test_upgrade_makes_both_required_columns_nullable_in_mysql_ddl(monkeypatch):
    ddl = _run_upgrade(
        {"allow_record": False, "allow_tuya_local": False},
        monkeypatch,
    )

    assert "MODIFY allow_record SMALLINT NULL" in ddl
    assert "MODIFY allow_tuya_local SMALLINT NULL" in ddl


def test_upgrade_is_noop_when_both_columns_are_already_nullable(monkeypatch):
    ddl = _run_upgrade(
        {"allow_record": True, "allow_tuya_local": True},
        monkeypatch,
    )

    assert ddl == ""
