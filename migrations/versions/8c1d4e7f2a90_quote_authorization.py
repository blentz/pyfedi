"""A quote this instance authorised is recorded, so /quote_boost_auth vouches only for those

Revision ID: 8c1d4e7f2a90
Revises: 23d65cdb8207
Create Date: 2026-10-01 20:00:00.000000

R205: process_quote_boost Accepted a FEP-044f QuoteRequest and then threw the decision
away, so the authorisation endpoint could confirm only that the quoted post existed.
"""
from alembic import op
import sqlalchemy as sa

revision = '8c1d4e7f2a90'
down_revision = '23d65cdb8207'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('quote_authorization',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=True),
    sa.Column('post_reply_id', sa.Integer(), nullable=True),
    sa.Column('quoting_uri', sa.String(length=1024), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['post_id'], ['post.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_reply_id'], ['post_reply.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('quote_authorization', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quote_authorization_post_id'), ['post_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_quote_authorization_post_reply_id'), ['post_reply_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_quote_authorization_quoting_uri'), ['quoting_uri'], unique=False)


def downgrade():
    with op.batch_alter_table('quote_authorization', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_quote_authorization_quoting_uri'))
        batch_op.drop_index(batch_op.f('ix_quote_authorization_post_reply_id'))
        batch_op.drop_index(batch_op.f('ix_quote_authorization_post_id'))

    op.drop_table('quote_authorization')
