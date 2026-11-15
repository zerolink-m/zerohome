"""Add hashed opaque session credentials while preserving legacy refresh sessions.

Revision ID: 47400_session_bearer
Revises: 47318ee8d9cf
"""
from alembic import op
import sqlalchemy as sa

revision = "47400_session_bearer"
down_revision = "47318ee8d9cf"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("token_hash", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("last_seen", sa.BigInteger(), nullable=True))
    # Legacy rows have no recoverable opaque token. Keep them for a one-time
    # exchange of their still-valid legacy session JWT.
    op.execute("UPDATE sessions SET last_seen = updated_at WHERE last_seen IS NULL")
    op.alter_column("sessions", "last_seen", existing_type=sa.BigInteger(), nullable=False)
    op.create_index("uq_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_last_seen", "sessions", ["last_seen"])


def downgrade():
    op.drop_index("ix_sessions_last_seen", table_name="sessions")
    op.drop_index("uq_sessions_token_hash", table_name="sessions")
    op.drop_column("sessions", "last_seen")
    op.drop_column("sessions", "token_hash")
