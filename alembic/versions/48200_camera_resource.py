"""Link imported cameras to their external resource.

Revision ID: 48200_camera_resource
Revises: 48100_connection_resources
"""
from alembic import op
import sqlalchemy as sa

revision = "48200_camera_resource"
down_revision = "48100_connection_resources"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("cams", "protocol", existing_type=sa.Enum("rtsp"), type_=sa.String(255), nullable=True)
    op.alter_column("cams", "address", existing_type=sa.String(255), nullable=True)
    op.alter_column("switches", "control_protocol", existing_type=sa.Enum("tuya_local", "tuya_cloud", "custom"), type_=sa.String(255), nullable=True)
    op.add_column("cams", sa.Column("connection_resource_id", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_cams_connection_resource", "cams", ["connection_resource_id"])
    op.create_foreign_key(
        "fk_cams_connection_resource", "cams", "connection_resources",
        ["connection_resource_id"], ["id"], ondelete="CASCADE",
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM cams WHERE protocol IS NULL OR protocol <> 'rtsp' OR address IS NULL")).scalar_one():
        raise RuntimeError("Remove or repair incompatible cameras before downgrading")
    if bind.execute(sa.text("SELECT COUNT(*) FROM switches WHERE control_protocol IS NULL OR control_protocol NOT IN ('tuya_local', 'tuya_cloud', 'custom')")).scalar_one():
        raise RuntimeError("Remove or repair incompatible switches before downgrading")
    op.drop_constraint("fk_cams_connection_resource", "cams", type_="foreignkey")
    op.drop_constraint("uq_cams_connection_resource", "cams", type_="unique")
    op.drop_column("cams", "connection_resource_id")
    op.alter_column("cams", "protocol", existing_type=sa.String(255), type_=sa.Enum("rtsp"), nullable=False)
    op.alter_column("cams", "address", existing_type=sa.String(255), nullable=False)
    op.alter_column("switches", "control_protocol", existing_type=sa.String(255), type_=sa.Enum("tuya_local", "tuya_cloud", "custom"), nullable=False)
