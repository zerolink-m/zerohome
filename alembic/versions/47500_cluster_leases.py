"""Add nodes and assignments tables for multi-node leases.

Revision ID: 47500_cluster_leases
Revises: 47400_session_bearer
"""
from alembic import op
import sqlalchemy as sa

revision = "47500_cluster_leases"
down_revision = "47400_session_bearer"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nodes",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("version", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_nodes_heartbeat_at", "nodes", ["heartbeat_at"])
    op.create_table(
        "assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("function", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_node_id", sa.String(64), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("function", "resource_id", name="uq_assignments_function_resource"),
    )
    op.create_index("ix_assignments_owner_node_id", "assignments", ["owner_node_id"])


def downgrade():
    op.drop_index("ix_assignments_owner_node_id", table_name="assignments")
    op.drop_table("assignments")
    op.drop_index("ix_nodes_heartbeat_at", table_name="nodes")
    op.drop_table("nodes")
