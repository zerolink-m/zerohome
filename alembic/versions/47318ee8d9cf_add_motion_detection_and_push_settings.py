"""add_motion_detection_and_push_settings

Revision ID: 47318ee8d9cf
Revises: 42051fbfaab8
Create Date: 2026-10-25 17:35:01.057197

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '47318ee8d9cf'
down_revision: Union[str, Sequence[str], None] = '42051fbfaab8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add motion_detection_allowed to users
    op.add_column('users', sa.Column('motion_detection_allowed', sa.SmallInteger(), server_default='0', nullable=False))

    # Add ONVIF flags to cams
    op.add_column('cams', sa.Column('onvif_enabled', sa.SmallInteger(), server_default='0', nullable=False))
    op.add_column('cams', sa.Column('onvif_snapshot_enabled', sa.SmallInteger(), server_default='0', nullable=False))
    op.add_column('cams', sa.Column('onvif_ptz_enabled', sa.SmallInteger(), server_default='0', nullable=False))

    # Create push_settings table
    op.create_table(
        'push_settings',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('device_type', sa.Enum('cams', 'switches', name='device_type_enum'), nullable=False),
        sa.Column('device_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.SmallInteger(), server_default='1', nullable=False),

        # Motion Detection settings
        sa.Column('motion_detection_enabled', sa.SmallInteger(), server_default='0', nullable=False),
        sa.Column('motion_min_confidence', sa.Float(), server_default='0.6', nullable=False),
        sa.Column('motion_min_object_size', sa.Float(), server_default='0.02', nullable=False),
        sa.Column('motion_cooldown_seconds', sa.Integer(), server_default='0', nullable=False),
        sa.Column('motion_detect_person', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('motion_detect_dog', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('motion_detect_cat', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('motion_detect_car', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('motion_license_plate_ocr', sa.SmallInteger(), server_default='1', nullable=False),

        # Other notification settings
        sa.Column('notify_offline', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('notify_online', sa.SmallInteger(), server_default='1', nullable=False),
        sa.Column('notify_state_change', sa.SmallInteger(), server_default='1', nullable=False),

        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('user_id', 'device_type', 'device_id', name='unique_user_device')
    )
    op.create_index('idx_push_settings_user', 'push_settings', ['user_id'])
    op.create_index('idx_push_settings_device', 'push_settings', ['device_type', 'device_id'])

    # Create motion_events table
    op.create_table(
        'motion_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('cam_id', sa.Integer(), nullable=False),
        sa.Column('detected_at', sa.BigInteger(), nullable=False),
        sa.Column('object_type', sa.Enum('person', 'dog', 'cat', 'car', name='object_type_enum'), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('object_size', sa.Float(), nullable=False),
        sa.Column('license_plate', sa.String(20), nullable=True),
        sa.Column('photo_filename', sa.String(255), nullable=False),
        sa.Column('bbox_x', sa.Integer(), nullable=True),
        sa.Column('bbox_y', sa.Integer(), nullable=True),
        sa.Column('bbox_width', sa.Integer(), nullable=True),
        sa.Column('bbox_height', sa.Integer(), nullable=True),
        sa.Column('notified_user_ids', sa.Text(), nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['cam_id'], ['cams.id'], ondelete='CASCADE')
    )
    op.create_index('idx_motion_events_cam_detected', 'motion_events', ['cam_id', 'detected_at'])
    op.create_index('idx_motion_events_object_type', 'motion_events', ['object_type'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_motion_events_object_type', 'motion_events')
    op.drop_index('idx_motion_events_cam_detected', 'motion_events')
    op.drop_table('motion_events')

    op.drop_index('idx_push_settings_device', 'push_settings')
    op.drop_index('idx_push_settings_user', 'push_settings')
    op.drop_table('push_settings')

    op.drop_column('cams', 'onvif_ptz_enabled')
    op.drop_column('cams', 'onvif_snapshot_enabled')
    op.drop_column('cams', 'onvif_enabled')

    op.drop_column('users', 'motion_detection_allowed')
