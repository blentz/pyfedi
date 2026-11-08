"""OAuth codes and tokens belong to the domain they were issued on

Revision ID: a7d3e9b2c4f1
Revises: f6c2a8e4b1d7
Create Date: 2026-11-08 20:00:00.000000

Owner ruling 2026-11-08 (plan W0.2): a token works only on the storefront it was issued on. Rows made before this
were all issued on the default domain, the only one W0.2's routes were open on in practice: they keep working there.
"""
import os
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa

revision = 'a7d3e9b2c4f1'
down_revision = 'f6c2a8e4b1d7'
branch_labels = None
depends_on = None


def _default_domain(bind) -> str:
    """The default storefront's host: its site_domain row, else SERVER_NAME as app.storefront normalises it."""
    domain = bind.execute(sa.text('SELECT domain FROM site_domain WHERE is_default ORDER BY id LIMIT 1')).scalar()
    if domain:
        return domain
    return (urlsplit('//' + (os.environ.get('SERVER_NAME') or 'localhost')).hostname or 'localhost').rstrip('.')


def upgrade():
    bind = op.get_bind()
    default = _default_domain(bind)
    for table in ('oauth_code', 'oauth_token'):
        op.add_column(table, sa.Column('domain', sa.String(255), nullable=True))
        bind.execute(sa.text(f'UPDATE {table} SET domain = :domain'), {'domain': default})
        op.alter_column(table, 'domain', nullable=False)


def downgrade():
    for table in ('oauth_token', 'oauth_code'):
        op.drop_column(table, 'domain')
