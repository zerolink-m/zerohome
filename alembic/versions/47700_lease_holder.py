"""Process token on assignment leases.

Revision ID: 47700_lease_holder
Revises: 47600_notify_photo
"""
from alembic import op
import sqlalchemy as sa

revision = "47700_lease_holder"
down_revision = "47600_notify_photo"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assignments", sa.Column("holder", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("assignments", "holder")
