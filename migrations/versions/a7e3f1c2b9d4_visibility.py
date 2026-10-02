"""Post and PostReply store their ActivityPub audience

Revision ID: a7e3f1c2b9d4
Revises: 8c1d4e7f2a90
Create Date: 2026-10-01 22:00:00.000000

Interop spec D6. First fork-only migration: it opens the 'fork' branch label
(spec D17) so upstream syncs merge heads instead of fighting over the chain.
Every existing row is public or unlisted (ingest refused the rest), and the two
cannot be told apart after the fact, so the backfill is 'public'.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7e3f1c2b9d4'
down_revision = '8c1d4e7f2a90'
branch_labels = ('fork',)
depends_on = None


def upgrade():
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('visibility', sa.String(length=10), nullable=False,
                                          server_default='public'))
            batch_op.create_index(batch_op.f(f'ix_{table}_visibility'), ['visibility'], unique=False)


def downgrade():
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f'ix_{table}_visibility'))
            batch_op.drop_column('visibility')
