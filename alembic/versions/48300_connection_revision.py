"""Add a monotonic revision for Ufanet synchronization."""
from alembic import op
import sqlalchemy as sa

revision = "48300_connection_revision"
down_revision = "48200_camera_resource"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("connections", sa.Column("sync_revision", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("connections", "sync_revision")
