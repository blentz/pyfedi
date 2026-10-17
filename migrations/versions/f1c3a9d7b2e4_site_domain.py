"""site_domain: every domain the site answers on (spec 2026-10-17 §3.1)

Revision ID: f1c3a9d7b2e4
Revises: e5b8d2f0a4c7
"""
import os

from alembic import op
import sqlalchemy as sa

revision = 'f1c3a9d7b2e4'
down_revision = 'e5b8d2f0a4c7'
branch_labels = None
depends_on = None

# Copied from app.storefront.registration_from_mode: migrations must not import app code.
REGISTRATION_FROM_MODE = {'Open': 'anyone', 'RequireApplication': 'anyone', 'Closed': 'admin'}


def upgrade():
    site_domain = op.create_table(
        'site_domain',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('scheme', sa.String(length=5), nullable=False),
        sa.Column('federates', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('registration', sa.String(length=10), server_default='anyone', nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_onion', sa.Boolean(), server_default='false', nullable=False),
        sa.CheckConstraint("registration IN ('anyone','staff','admin')", name='ck_site_domain_registration'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('domain'),
    )
    op.create_index('ix_site_domain_one_default', 'site_domain', ['is_default'], unique=True,
                    postgresql_where=sa.text('is_default'))

    domain = (os.environ.get('SERVER_NAME') or 'localhost').lower()
    protocol = os.environ.get('HTTP_PROTOCOL') or 'https'
    mode = op.get_bind().execute(sa.text('SELECT registration_mode FROM site ORDER BY id LIMIT 1')).scalar()
    op.bulk_insert(site_domain, [{
        'domain': domain,
        'scheme': 'https' if protocol in ('https', 'mixed') else protocol,
        'federates': True,
        'registration': REGISTRATION_FROM_MODE.get(mode, 'admin'),
        'is_default': True,
        'is_onion': domain.endswith('.onion'),
    }])


def downgrade():
    op.drop_index('ix_site_domain_one_default', table_name='site_domain', postgresql_where=sa.text('is_default'))
    op.drop_table('site_domain')
