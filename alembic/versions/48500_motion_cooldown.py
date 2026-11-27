"""Remove the per-user motion notification cooldown."""
from alembic import op
import sqlalchemy as sa

revision = "48500_motion_cooldown"
down_revision = "48400_local_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("push_settings", "motion_cooldown_seconds")


def downgrade():
    op.add_column(
        "push_settings",
        sa.Column("motion_cooldown_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
