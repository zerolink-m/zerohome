"""Fresh MySQL installs must accommodate the unchanged historical revision IDs."""
from pathlib import Path
from io import StringIO

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.db.migration_impl import ZeroHomeMySQLImpl


def test_mysql_version_table_fits_every_revision_and_keeps_primary_key():
    context = MigrationContext.configure(dialect=mysql.dialect())
    assert isinstance(context.impl, ZeroHomeMySQLImpl)
    table = context._version
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    revisions = [revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions()]
    assert max(map(len, revisions)) > 32  # this is why the override is required
    assert table.c.version_num.type.length >= max(map(len, revisions))
    assert table.c.version_num.nullable is False
    assert list(table.primary_key.columns) == [table.c.version_num]
    assert "VARCHAR(128)" in str(CreateTable(table).compile(dialect=mysql.dialect()))


def test_mysql_offline_creation_uses_wide_version_column():
    output = StringIO()
    context = MigrationContext.configure(dialect=mysql.dialect(), opts={
        "as_sql": True, "output_buffer": output,
    })
    context._ensure_version_table()
    assert "VARCHAR(128)" in output.getvalue()
