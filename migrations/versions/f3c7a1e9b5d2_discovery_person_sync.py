"""discovery_person_sync holds the Pixelfed accounts the instance actor keeps seeded into microblogs

Revision ID: f3c7a1e9b5d2
Revises: e8b4d2a6c1f3
Create Date: 2026-10-29 23:00:00.000000

Pixelfed seeding (spec 2026-10-29): one row per followed account, discovery_sync's twin for a person. Built by
`flask sync_discovery`; nothing is followed until an admin sets discovery_people_per_host.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3c7a1e9b5d2'
down_revision = 'e8b4d2a6c1f3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_person_sync',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('entry_id', sa.Integer(), nullable=True),
        sa.Column('follow_target', sa.String(length=1024), nullable=False),
        sa.Column('follow_uuid', sa.String(length=36), nullable=True),
        sa.Column('follow_state', sa.String(length=10), server_default='none', nullable=False),
        sa.Column('followed_at', sa.DateTime(), nullable=True),
        sa.Column('last_polled_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entry_id'], ['discovery_entry.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('user_id'),
    )
    with op.batch_alter_table('discovery_person_sync', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discovery_person_sync_follow_uuid'), ['follow_uuid'], unique=False)


def downgrade():
    with op.batch_alter_table('discovery_person_sync', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discovery_person_sync_follow_uuid'))
    op.drop_table('discovery_person_sync')
