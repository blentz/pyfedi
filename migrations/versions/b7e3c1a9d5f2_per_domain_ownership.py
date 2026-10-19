"""each local user and community belongs to a hosted domain; post and reply local_only flags

Revision ID: b7e3c1a9d5f2
Revises: a6d2f8c4e1b7
Create Date: 2026-10-19 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e3c1a9d5f2'
down_revision = 'a6d2f8c4e1b7'
branch_labels = None
depends_on = None

# Every existing local account and community moves to the default domain; remote rows stay NULL.
BACKFILL_USERS = 'UPDATE "user" SET site_domain_id = (SELECT id FROM site_domain WHERE is_default) WHERE ap_id IS NULL'
BACKFILL_COMMUNITIES = ('UPDATE "community" SET site_domain_id = (SELECT id FROM site_domain WHERE is_default) '
                        'WHERE ap_id IS NULL')


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_domain_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_user_site_domain_id', 'site_domain', ['site_domain_id'], ['id'])
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_domain_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_community_site_domain_id', 'site_domain', ['site_domain_id'], ['id'])
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_only', sa.Boolean(), server_default='false', nullable=False))
    with op.batch_alter_table('post_reply', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_only', sa.Boolean(), server_default='false', nullable=False))

    op.execute(BACKFILL_USERS)
    op.execute(BACKFILL_COMMUNITIES)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.create_index('ix_user_domain_lower_name', ['site_domain_id', sa.literal_column('lower(user_name)')],
                              unique=True, postgresql_where=sa.text('ap_id IS NULL'))
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.create_index('ix_community_domain_lower_name', ['site_domain_id', sa.literal_column('lower(name)')],
                              unique=True, postgresql_where=sa.text('ap_id IS NULL'))


def downgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.drop_index('ix_community_domain_lower_name')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index('ix_user_domain_lower_name')
    with op.batch_alter_table('post_reply', schema=None) as batch_op:
        batch_op.drop_column('local_only')
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.drop_column('local_only')
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.drop_constraint('fk_community_site_domain_id', type_='foreignkey')
        batch_op.drop_column('site_domain_id')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('fk_user_site_domain_id', type_='foreignkey')
        batch_op.drop_column('site_domain_id')
