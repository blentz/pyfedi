"""payment link-outs: an offer can be a link to somewhere else that takes the money

Revision ID: c3f7a9e1d5b8
Revises: b2e6c4a8d1f3
Create Date: 2026-11-06 08:00:00.000000

Plan docs/superpowers/plans/2026-11-06-monetization-phase-2.md. The downgrade removes link-outs with the column, since a
link-out is nothing without its address.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3f7a9e1d5b8'
down_revision = 'b2e6c4a8d1f3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('offer', sa.Column('url', sa.String(length=1024)))


def downgrade():
    op.get_bind().execute(sa.text("DELETE FROM offer WHERE kind = 'link'"))
    op.drop_column('offer', 'url')
