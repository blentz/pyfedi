"""a dark-mode logo beside the logo, for the site and each domain

Revision ID: d7f2b9c4e1a3
Revises: c3e9a7d1f5b2
Create Date: 2026-11-02 12:00:00.000000

The logo pages show in dark mode. `logo_dark` is the upload (an SVG as uploaded, else a PNG of at most 100
pixels) and `logo_dark_152` what the header shows for a raster upload, as `logo` and `logo_152` are for the logo.
'' or NULL means none: dark mode then shows the logo.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7f2b9c4e1a3'
down_revision = 'c3e9a7d1f5b2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('site') as batch_op:
        batch_op.add_column(sa.Column('logo_dark', sa.String(length=40), nullable=True, server_default=''))
        batch_op.add_column(sa.Column('logo_dark_152', sa.String(length=40), nullable=True, server_default=''))
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.add_column(sa.Column('logo_dark', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('logo_dark_152', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.drop_column('logo_dark_152')
        batch_op.drop_column('logo_dark')
    with op.batch_alter_table('site') as batch_op:
        batch_op.drop_column('logo_dark_152')
        batch_op.drop_column('logo_dark')
