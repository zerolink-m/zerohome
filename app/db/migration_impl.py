"""Alembic's supported version-table hook for our historical revision IDs."""
from alembic.ddl.mysql import MySQLImpl
from sqlalchemy import String


class ZeroHomeMySQLImpl(MySQLImpl):
    # Register only when imported by alembic/env.py. Several existing revision
    # identifiers exceed Alembic's VARCHAR(32) default; never rename those IDs.
    __dialect__ = "mysql"

    def version_table_impl(self, **kwargs):
        table = super().version_table_impl(**kwargs)
        table.c.version_num.type = String(128)
        return table
