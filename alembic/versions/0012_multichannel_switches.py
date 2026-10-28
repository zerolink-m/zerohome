"""multichannel switches: is_main, parent, external_switch_channel

Revision ID: 0012_multichannel_switches
Revises: 0011_connections_and_auto_switches
Create Date: 2026-08-08

One physical Tuya device (e.g. a 2-gang switch) exposes several DP codes
(switch_1, switch_2). We materialise each channel as its own `switches` row
plus a "master" row that maps to the whole physical device:

    is_main = 1             — master row for the physical device
    is_main = 0             — a channel row
    parent  = <master.id>   — set on children; NULL on master
    external_switch_channel — DP code (e.g. "switch_1"); NULL on master

`external_id` continues to hold the Tuya device_id (no ":switch_N" suffix
tacked on) — it is identical for the master and all its children. The
old (connection_id, external_id) UNIQUE is replaced with
(connection_id, external_id, external_switch_channel) so both a master
row (channel=NULL) and each of its children fit under the same device_id.

Downgrade drops the extras; auto rows that lose their invariant (children
without parent, masters that were multi-row-per-device) are cleaned up
before re-adding the old UNIQUE.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_multichannel_switches"
down_revision = "0011_connections_and_auto_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) new columns
    op.add_column(
        "switches",
        sa.Column("is_main", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "switches",
        sa.Column(
            "parent",
            sa.Integer(),
            sa.ForeignKey("switches.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "switches",
        sa.Column("external_switch_channel", sa.String(128), nullable=True),
    )

    # 2) drop old UNIQUE (connection_id, external_id) — a device now has many rows
    op.drop_constraint("uq_switches_connection_external", "switches", type_="unique")
    op.create_unique_constraint(
        "uq_switches_connection_external_channel",
        "switches",
        ["connection_id", "external_id", "external_switch_channel"],
    )

    # 3) backfill: promote every pre-existing auto row to a master row. Before
    # this migration there was exactly one auto row per physical device — the
    # role a master row plays now. Children will be recreated on the next
    # sync tick from `get_device_specifications`; deleting them here would
    # only orphan invitations/sharings that never existed anyway (there was
    # nothing to share to at the child level yet).
    #
    # This has to run BEFORE the new CHECK is added, otherwise MySQL evaluates
    # the constraint against the still-`is_main=0` rows and errors out with
    # 3819 (Check constraint 'ck_switches_added_by_consistency' is violated).
    op.execute(
        "UPDATE switches SET is_main = 1 "
        "WHERE added_by = 'auto' AND external_id IS NOT NULL"
    )

    # 4) drop the old added_by CHECK; replace with the multichannel-aware version.
    # Invariants:
    #   added_by='user'  ↔  connection_id IS NULL AND external_id IS NULL
    #                       AND external_switch_channel IS NULL AND is_main = 0
    #                       AND parent IS NULL
    #   added_by='auto'  ↔  connection_id IS NOT NULL AND external_id IS NOT NULL
    #   auto AND is_main=1  →  parent IS NULL AND external_switch_channel IS NULL
    #   auto AND is_main=0  →  parent IS NOT NULL AND external_switch_channel IS NOT NULL
    op.drop_constraint("ck_switches_added_by_consistency", "switches", type_="check")
    op.create_check_constraint(
        "ck_switches_added_by_consistency",
        "switches",
        "("
        "  added_by = 'user' AND connection_id IS NULL AND external_id IS NULL "
        "  AND external_switch_channel IS NULL AND is_main = 0 AND parent IS NULL"
        ") OR ("
        "  added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL "
        "  AND ("
        "    (is_main = 1 AND parent IS NULL AND external_switch_channel IS NULL)"
        "    OR"
        "    (is_main = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)"
        "  )"
        ")",
    )

    op.create_index("ix_switches_parent", "switches", ["parent"])


def downgrade() -> None:
    op.drop_index("ix_switches_parent", table_name="switches")

    op.drop_constraint("ck_switches_added_by_consistency", "switches", type_="check")
    op.create_check_constraint(
        "ck_switches_added_by_consistency",
        "switches",
        "(added_by = 'user' AND connection_id IS NULL AND external_id IS NULL) "
        "OR (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL)",
    )

    # Multichannel data is incompatible with the old schema — drop children,
    # collapse masters to a single row per device.
    op.execute("DELETE FROM switches WHERE parent IS NOT NULL")
    op.execute("DELETE FROM switches WHERE is_main = 1 AND added_by = 'auto'")

    op.drop_constraint("uq_switches_connection_external_channel", "switches", type_="unique")
    op.create_unique_constraint(
        "uq_switches_connection_external",
        "switches",
        ["connection_id", "external_id"],
    )

    op.drop_column("switches", "external_switch_channel")
    op.drop_column("switches", "parent")
    op.drop_column("switches", "is_main")
