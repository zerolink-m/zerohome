"""Allow administrators to grant custom switch access to local addresses."""
from alembic import op
import sqlalchemy as sa

revision = "48400_local_connections"
down_revision = "48300_connection_revision"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("local_connections_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("users", "local_connections_allowed")
