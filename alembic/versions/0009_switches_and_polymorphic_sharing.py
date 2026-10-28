"""switches table, users.allowtuyalocal, polymorphic device_type/device_id on invitations/sharings

Revision ID: 0009_switches_and_polymorphic_sharing
Revises: 0008_fk_ondelete_cascade
Create Date: 2026-08-05

Downgrade note: rows in invitations/sharings with device_type='switches' cannot be
represented in the old cam_id/cid schema and are DELETED on downgrade (data loss by
design, since the switches table itself is dropped in the same downgrade).
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_switches_and_polymorphic_sharing"
down_revision = "0008_fk_ondelete_cascade"
branch_labels = None
depends_on = None

DEVICE_TYPE_ENUM = sa.Enum("cams", "switches", name="device_type")


def _fk_name(bind, table: str, column: str) -> str:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if fk["constrained_columns"] == [column]:
            return fk["name"]
    raise RuntimeError(f"foreign key on {table}.{column} not found")


def _unique_constraint_name(bind, table: str, columns: list[str]) -> str | None:
    inspector = sa.inspect(bind)
    for uc in inspector.get_unique_constraints(table):
        if uc["column_names"] == columns:
            return uc["name"]
    return None


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "switches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ownuser", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("notify", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "controlproto",
            sa.Enum("tuya_local", "tuya_cloud", "custom", name="switch_control_proto"),
            nullable=False,
        ),
        sa.Column("controlmeta", sa.JSON(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("state_updated_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.BigInteger(), nullable=False),
    )

    op.add_column(
        "users",
        sa.Column("allowtuyalocal", sa.SmallInteger(), nullable=False, server_default="0"),
    )

    for table, old_col in (("invitations", "cam_id"), ("sharings", "cid")):
        op.add_column(table, sa.Column("device_type", DEVICE_TYPE_ENUM, nullable=True))
        op.add_column(table, sa.Column("device_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text(f"UPDATE {table} SET device_type = 'cams', device_id = {old_col}")
        )
        op.alter_column(table, "device_type", existing_type=DEVICE_TYPE_ENUM, nullable=False)
        op.alter_column(table, "device_id", existing_type=sa.Integer(), nullable=False)

        old_fk = _fk_name(bind, table, old_col)
        op.drop_constraint(old_fk, table, type_="foreignkey")

    uq_name = _unique_constraint_name(bind, "invitations", ["cam_id", "email"])
    if uq_name is None:
        uq_name = "uq_invitations_cam_id_email"
    op.drop_constraint(uq_name, "invitations", type_="unique")
    op.create_unique_constraint(
        "uq_invitations_device_email", "invitations", ["device_type", "device_id", "email"]
    )

    op.create_index(
        "ix_invitations_device_type_device_id", "invitations", ["device_type", "device_id"]
    )
    op.create_index(
        "ix_sharings_device_type_device_id", "sharings", ["device_type", "device_id"]
    )

    op.drop_column("invitations", "cam_id")
    op.drop_column("sharings", "cid")


def downgrade() -> None:
    op.add_column("invitations", sa.Column("cam_id", sa.Integer(), nullable=True))
    op.add_column("sharings", sa.Column("cid", sa.Integer(), nullable=True))

    op.execute(sa.text("DELETE FROM invitations WHERE device_type != 'cams'"))
    op.execute(sa.text("DELETE FROM sharings WHERE device_type != 'cams'"))
    op.execute(sa.text("UPDATE invitations SET cam_id = device_id"))
    op.execute(sa.text("UPDATE sharings SET cid = device_id"))

    op.alter_column("invitations", "cam_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("sharings", "cid", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_sharings_device_type_device_id", table_name="sharings")
    op.drop_index("ix_invitations_device_type_device_id", table_name="invitations")

    op.drop_constraint("uq_invitations_device_email", "invitations", type_="unique")
    op.create_unique_constraint("uq_invitations_cam_id_email", "invitations", ["cam_id", "email"])

    op.create_foreign_key(
        "fk_invitations_cam_id_cams", "invitations", "cams", ["cam_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_sharings_cid_cams", "sharings", "cams", ["cid"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_invitations_cam_id", "invitations", ["cam_id"])
    op.create_index("ix_sharings_cid", "sharings", ["cid"])

    op.drop_column("invitations", "device_id")
    op.drop_column("invitations", "device_type")
    op.drop_column("sharings", "device_id")
    op.drop_column("sharings", "device_type")

    op.drop_column("users", "allowtuyalocal")

    op.drop_table("switches")
