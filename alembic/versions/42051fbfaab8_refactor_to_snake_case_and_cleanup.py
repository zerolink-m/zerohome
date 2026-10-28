"""refactor_to_snake_case_and_cleanup

Revision ID: 42051fbfaab8
Revises: 0012_multichannel_switches
Create Date: 2026-08-12 21:18:35.071096
"""
from alembic import op

revision = '42051fbfaab8'
down_revision = '0012_multichannel_switches'


def upgrade() -> None:
    # --- USERS: allowrecorde → allowrecord ---
    op.execute('ALTER TABLE users CHANGE COLUMN allowrecorde allowrecord SMALLINT NOT NULL DEFAULT 0')

    # --- SWITCHES: controlproto → control_proto, is_main → is_master ---
    # Drop CHECK constraint first (references is_main)
    op.drop_constraint('ck_switches_added_by_consistency', 'switches', type_='check')

    # Rename columns
    op.execute("ALTER TABLE switches CHANGE COLUMN controlproto control_proto ENUM('tuya_local', 'tuya_cloud', 'custom') COLLATE utf8mb4_unicode_ci NOT NULL")
    op.execute('ALTER TABLE switches CHANGE COLUMN is_main is_master SMALLINT NOT NULL DEFAULT 0')

    # Recreate CHECK with is_master
    op.execute("""
        ALTER TABLE switches ADD CONSTRAINT ck_switches_added_by_consistency CHECK ((
          (added_by = 'user' AND connection_id IS NULL AND external_id IS NULL
           AND external_switch_channel IS NULL AND is_master = 0 AND parent IS NULL)
          OR
          (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL
           AND ((is_master = 1 AND parent IS NULL AND external_switch_channel IS NULL)
                OR (is_master = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)))
        ))
    """)

    # --- SESSIONS: add is_pwa ---
    op.execute('ALTER TABLE sessions ADD COLUMN is_pwa SMALLINT NOT NULL DEFAULT 0')

    # --- INVITATIONS: invited_at/responded_at → added_at/updated_at ---
    op.execute('ALTER TABLE invitations ADD COLUMN added_at BIGINT, ADD COLUMN updated_at BIGINT')
    op.execute('UPDATE invitations SET added_at = invited_at, updated_at = COALESCE(responded_at, invited_at)')
    op.execute('ALTER TABLE invitations MODIFY added_at BIGINT NOT NULL, MODIFY updated_at BIGINT NOT NULL')
    op.execute('ALTER TABLE invitations DROP COLUMN invited_at, DROP COLUMN responded_at')

    # --- Drop unnecessary indexes ---
    op.drop_index('ix_invitations_device_type_device_id', table_name='invitations')
    op.drop_index('ix_sharings_device_type_device_id', table_name='sharings')


def downgrade() -> None:
    op.create_index('ix_sharings_device_type_device_id', 'sharings', ['device_type', 'device_id'])
    op.create_index('ix_invitations_device_type_device_id', 'invitations', ['device_type', 'device_id'])

    op.execute('ALTER TABLE invitations ADD COLUMN invited_at BIGINT, ADD COLUMN responded_at BIGINT')
    op.execute("UPDATE invitations SET invited_at = added_at, responded_at = CASE WHEN status = 'accepted' THEN updated_at ELSE NULL END")
    op.execute('ALTER TABLE invitations MODIFY invited_at BIGINT NOT NULL')
    op.execute('ALTER TABLE invitations DROP COLUMN added_at, DROP COLUMN updated_at')

    op.execute('ALTER TABLE sessions DROP COLUMN is_pwa')

    op.drop_constraint('ck_switches_added_by_consistency', 'switches', type_='check')
    op.execute('ALTER TABLE switches CHANGE COLUMN is_master is_main SMALLINT NOT NULL DEFAULT 0')
    op.execute("ALTER TABLE switches CHANGE COLUMN control_proto controlproto ENUM('tuya_local', 'tuya_cloud', 'custom') COLLATE utf8mb4_unicode_ci NOT NULL")
    op.execute("""
        ALTER TABLE switches ADD CONSTRAINT ck_switches_added_by_consistency CHECK ((
          (added_by = 'user' AND connection_id IS NULL AND external_id IS NULL
           AND external_switch_channel IS NULL AND is_main = 0 AND parent IS NULL)
          OR
          (added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL
           AND ((is_main = 1 AND parent IS NULL AND external_switch_channel IS NULL)
                OR (is_main = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)))
        ))
    """)

    op.execute('ALTER TABLE users CHANGE COLUMN allowrecord allowrecorde SMALLINT NOT NULL DEFAULT 0')
