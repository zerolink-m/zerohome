"""Normalize persisted global and per-node runtime configuration.

Revision ID: 47900_runtime_configuration
Revises: 47800_schema_names
"""
from alembic import op
import sqlalchemy as sa

_quoted = sa.sql.quoted_name

revision = "47900_runtime_configuration"
down_revision = "47800_schema_names"
branch_labels = None
depends_on = None


def upgrade():
    # Keep the legacy identity/configuration payload while moving operational
    # values into normalized rows. At 47800 the nodes table has no payload yet.
    node_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("nodes")}
    if "configuration" not in node_columns:
        op.add_column("nodes", sa.Column("configuration", sa.JSON(), nullable=True))
        op.execute("UPDATE nodes SET configuration = JSON_OBJECT() WHERE configuration IS NULL")
        op.alter_column("nodes", "configuration", existing_type=sa.JSON(), nullable=False)

    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "global_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_table(
        "node_settings",
        sa.Column("node_id", sa.String(64), sa.ForeignKey("nodes.node_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_table(
        "node_capabilities",
        sa.Column("node_id", sa.String(64), sa.ForeignKey("nodes.node_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("capability", sa.String(64), primary_key=True),
        sa.Column("supported", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("capacity", sa.Integer(), nullable=True),
    )

    bind = op.get_bind()
    now = 0
    bind.execute(sa.insert(sa.table("system_settings",
                                    sa.column("id", sa.Integer),
                                    sa.column("revision", sa.BigInteger),
                                    sa.column("updated_at", sa.BigInteger))),
                 {"id": 1, "revision": 0, "updated_at": now})

    # Move the in-progress key/value format without changing any values.
    if sa.inspect(bind).has_table("global_config"):
        old = sa.table("global_config", sa.column(_quoted("key", True), sa.String), sa.column("value", sa.JSON))
        new = sa.table("global_settings", sa.column(_quoted("key", True), sa.String), sa.column("value", sa.JSON))
        for row in bind.execute(sa.select(old.c.key, old.c.value)).mappings():
            bind.execute(sa.insert(new).values(key=row["key"], value=row["value"]))

    # Existing nodes and assignments are kept. Migrate node-local JSON and
    # heartbeat capability snapshots into explicit rows for administrative use.
    nodes_table = sa.table("nodes", sa.column("node_id", sa.String), sa.column("capabilities", sa.JSON),
                           sa.column("configuration", sa.JSON))
    nodes = bind.execute(sa.select(nodes_table)).mappings()
    node_settings = sa.table("node_settings", sa.column("node_id", sa.String),
                             sa.column(_quoted("key", True), sa.String), sa.column("value", sa.JSON))
    node_caps = sa.table("node_capabilities", sa.column("node_id", sa.String),
                         sa.column("capability", sa.String), sa.column("supported", sa.Boolean),
                         sa.column("enabled", sa.Boolean), sa.column("capacity", sa.Integer))
    for node in nodes:
        config = node["configuration"] or {}
        if isinstance(config, str):
            import json
            config = json.loads(config)
        for key, value in (config.get("settings") or {}).items():
            bind.execute(sa.insert(node_settings).values(node_id=node["node_id"], key=key, value=value))
        caps = node["capabilities"] or {}
        if isinstance(caps, str):
            import json
            caps = json.loads(caps)
        enabled_caps = config.get("enabled_capabilities") or caps
        for capability, is_enabled in {**caps, **enabled_caps}.items():
            bind.execute(sa.insert(node_caps).values(
                node_id=node["node_id"], capability=capability,
                supported=True, enabled=bool(is_enabled), capacity=None,
            ))


def downgrade():
    op.drop_table("node_capabilities")
    op.drop_table("node_settings")
    op.drop_table("global_settings")
    op.drop_table("system_settings")
