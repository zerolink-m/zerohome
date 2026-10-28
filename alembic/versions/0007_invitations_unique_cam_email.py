"""unique constraint on invitations (cam_id, email)

Revision ID: 0007_invitations_unique_cam_email
Revises: 0006_push_notify_invitations_sharings
Create Date: 2026-08-05
"""

from alembic import op

revision = "0007_invitations_unique_cam_email"
down_revision = "0006_push_notify_invitations_sharings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_invitations_cam_id_email",
        "invitations",
        ["cam_id", "email"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_invitations_cam_id_email", "invitations", type_="unique")
