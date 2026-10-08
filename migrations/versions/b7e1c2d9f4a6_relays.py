"""relays

Revision ID: b7e1c2d9f4a6
Revises: a3c9e5f1b7d4
"""
import sqlalchemy as sa
from alembic import op

revision = 'b7e1c2d9f4a6'
down_revision = 'a3c9e5f1b7d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('relay',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('url', sa.String(length=2048), nullable=False),
                    sa.Column('style', sa.String(length=16), nullable=False),
                    sa.Column('inbox_url', sa.String(length=2048), nullable=False),
                    sa.Column('actor_id', sa.String(length=2048), nullable=True),
                    sa.Column('public_key', sa.Text(), nullable=True),
                    sa.Column('follow_activity_id', sa.String(length=2048), nullable=False),
                    sa.Column('state', sa.String(length=16), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('answered_at', sa.DateTime(), nullable=True),
                    sa.Column('last_error', sa.String(length=1024), nullable=True),
                    sa.PrimaryKeyConstraint('id'),
                    sa.UniqueConstraint('url'))
    op.create_index('ix_relay_actor_id', 'relay', ['actor_id'])
    op.add_column('post', sa.Column('relay_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_post_relay_id', 'post', 'relay', ['relay_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_post_relay_id', 'post', ['relay_id'], postgresql_where=sa.text('relay_id IS NOT NULL'))


def downgrade():
    op.drop_index('ix_post_relay_id', table_name='post')
    op.drop_constraint('fk_post_relay_id', 'post', type_='foreignkey')
    op.drop_column('post', 'relay_id')
    op.drop_index('ix_relay_actor_id', table_name='relay')
    op.drop_table('relay')
