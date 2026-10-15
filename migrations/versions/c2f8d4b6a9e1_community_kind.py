"""community.kind: discussion, microblogs, video_channel or podcast

Spec 2026-10-14 §3.1, first read by §6.2: a video_channel community's Group names its owner Person in attributedTo,
which PeerTube needs to subscribe. Every row starts as 'discussion'. Setting 'microblogs' on the local microblogs
community, and the forms that set the kind, are step 4 of the spec's build order.

Revision ID: c2f8d4b6a9e1
Revises: 3d52b8a19cf5
Create Date: 2026-10-15 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2f8d4b6a9e1'
down_revision = '3d52b8a19cf5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False, server_default='discussion'))


def downgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.drop_column('kind')
