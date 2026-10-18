"""indexes on lower(user.email) and lower(user.ap_profile_id), for the login lookup

Revision ID: a6d2f8c4e1b7
Revises: f1c3a9d7b2e4
Create Date: 2026-10-18 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a6d2f8c4e1b7'
down_revision = 'f1c3a9d7b2e4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.create_index('idx_user_email_lower', [sa.literal_column('lower(email)')], unique=False)
        batch_op.create_index('idx_user_ap_profile_id_lower', [sa.literal_column('lower(ap_profile_id)')],
                              unique=False)


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index('idx_user_ap_profile_id_lower')
        batch_op.drop_index('idx_user_email_lower')
