"""Connected accounts: a copy's progress across attempts, and room for the unconfirmed state

Revision ID: f3b7d1a9c5e2
Revises: e6a2c9d4f8b1
Create Date: 2026-11-14 20:00:00.000000

Spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md §19.2 (ruling C13): `progress` keeps what a
send did so far (Bluesky's record key, YouTube's upload address), and `state` grows to hold `unconfirmed`.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3b7d1a9c5e2'
down_revision = 'e6a2c9d4f8b1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('social_crosspost') as batch_op:
        batch_op.add_column(sa.Column('progress', sa.JSON(), nullable=True))
        batch_op.alter_column('state', existing_type=sa.String(length=10), type_=sa.String(length=20),
                              existing_nullable=False)


def downgrade():
    op.execute("UPDATE social_crosspost SET state = 'failed' WHERE state = 'unconfirmed'")
    with op.batch_alter_table('social_crosspost') as batch_op:
        batch_op.alter_column('state', existing_type=sa.String(length=20), type_=sa.String(length=10),
                              existing_nullable=False)
        batch_op.drop_column('progress')
