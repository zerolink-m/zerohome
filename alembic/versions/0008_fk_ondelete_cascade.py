"""sessions.uid and cams.ownuser -> users.id: add ondelete=CASCADE

Revision ID: 0008_fk_ondelete_cascade
Revises: 0007_invitations_unique_cam_email
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_fk_ondelete_cascade"
down_revision = "0007_invitations_unique_cam_email"
branch_labels = None
depends_on = None


def _fk_name(bind, table: str, column: str) -> str:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if fk["constrained_columns"] == [column]:
            return fk["name"]
    raise RuntimeError(f"foreign key on {table}.{column} not found")


def upgrade() -> None:
    bind = op.get_bind()

    sessions_fk = _fk_name(bind, "sessions", "uid")
    op.drop_constraint(sessions_fk, "sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_sessions_uid_users", "sessions", "users", ["uid"], ["id"], ondelete="CASCADE"
    )

    cams_fk = _fk_name(bind, "cams", "ownuser")
    op.drop_constraint(cams_fk, "cams", type_="foreignkey")
    op.create_foreign_key(
        "fk_cams_ownuser_users", "cams", "users", ["ownuser"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("fk_cams_ownuser_users", "cams", type_="foreignkey")
    op.create_foreign_key("fk_cams_ownuser_users", "cams", "users", ["ownuser"], ["id"])

    op.drop_constraint("fk_sessions_uid_users", "sessions", type_="foreignkey")
    op.create_foreign_key("fk_sessions_uid_users", "sessions", "users", ["uid"], ["id"])
