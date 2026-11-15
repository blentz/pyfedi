"""Connected accounts: a domain's own apps, and which app issued a connection's tokens

Revision ID: e6a2c9d4f8b1
Revises: d4f8a2c6e1b9
Create Date: 2026-11-14 18:00:00.000000

Spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md §18 (rulings C8, C10, C11). Connections made
before this have no recorded app: they are the instance's (app_domain_id NULL), and their app id is filled at their
next call.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6a2c9d4f8b1'
down_revision = 'd4f8a2c6e1b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'connection_app',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_domain_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('client_id', sa.String(length=255), nullable=False),
        sa.Column('sealed_secret', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['site_domain_id'], ['site_domain.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_domain_id', 'kind', name='uq_connection_app_domain_kind'),
    )
    op.create_index('ix_connection_app_site_domain_id', 'connection_app', ['site_domain_id'])
    with op.batch_alter_table('social_connection', schema=None) as batch_op:
        batch_op.add_column(sa.Column('app_domain_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('app_client_id', sa.String(length=255), nullable=True))
        batch_op.create_foreign_key('social_connection_app_domain_id_fkey', 'site_domain', ['app_domain_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('social_connection', schema=None) as batch_op:
        batch_op.drop_constraint('social_connection_app_domain_id_fkey', type_='foreignkey')
        batch_op.drop_column('app_client_id')
        batch_op.drop_column('app_domain_id')
    op.drop_table('connection_app')
