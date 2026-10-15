"""user.ap_own_inbox_url: a remote actor's own inbox, where the Accept or Reject of its Follow goes

Spec 2026-10-14 §10 A12. ap_inbox_url holds the shared inbox when the actor has one, and PeerTube ignores an Accept
there. Nullable and not backfilled: an existing actor gets it on its next refresh, and is answered at ap_inbox_url
until then.

Revision ID: b7e3c9a1f5d2
Revises: c2f8d4b6a9e1
Create Date: 2026-10-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e3c9a1f5d2'
down_revision = 'c2f8d4b6a9e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ap_own_inbox_url', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('ap_own_inbox_url')
