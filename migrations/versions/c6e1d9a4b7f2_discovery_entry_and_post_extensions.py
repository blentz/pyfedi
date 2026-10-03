"""discovery_entry holds what opt-in directories list; post.extensions holds per-post fork data

Revision ID: c6e1d9a4b7f2
Revises: b5d2c8e1f7a3
Create Date: 2026-10-03 12:00:00.000000

Interop spec D24. discovery_entry is the search/pre-load index built daily by
`flask refresh_discovery`; actor_url is unique because the refresh upserts on it.
post.extensions is the first piece of D8's content-kind layout: Castopod credits
live at extensions['podcast']['credits']. Nullable, no backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c6e1d9a4b7f2'
down_revision = 'b5d2c8e1f7a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_entry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('actor_url', sa.String(length=1024), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('avatar_url', sa.String(length=1024), nullable=True),
        sa.Column('followers', sa.Integer(), server_default='0', nullable=False),
        sa.Column('nsfw', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('discovery_entry', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discovery_entry_actor_url'), ['actor_url'], unique=True)
        batch_op.create_index(batch_op.f('ix_discovery_entry_host'), ['host'], unique=False)
        batch_op.create_index(batch_op.f('ix_discovery_entry_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_discovery_entry_last_seen'), ['last_seen'], unique=False)
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.add_column(sa.Column('extensions', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.drop_column('extensions')
    with op.batch_alter_table('discovery_entry', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discovery_entry_last_seen'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_kind'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_host'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_actor_url'))
    op.drop_table('discovery_entry')
