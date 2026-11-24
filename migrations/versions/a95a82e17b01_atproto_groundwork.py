"""ATProto groundwork: a Bluesky connection's variant and identity, its records, the stream cursor, login-only posts

Revision ID: a95a82e17b01
Revises: f3b7d1a9c5e2
Create Date: 2026-11-23 18:00:00.000000

Spec docs/superpowers/specs/2026-11-23-atproto-federation-design.md §5. Every Bluesky connection so far was made by
OAuth, so each becomes the `oauth` variant; the `hosted` variant (an account on PieFed's own PDS) comes later.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a95a82e17b01'
down_revision = 'f3b7d1a9c5e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('social_connection') as batch_op:
        batch_op.add_column(sa.Column('is_identity', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('import_own', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('slot_state', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('slot_pinned', sa.Boolean(), nullable=False, server_default='false'))
    op.create_index('uq_social_connection_identity', 'social_connection', ['user_id'], unique=True,
                    postgresql_where=sa.text('is_identity'))
    op.execute("UPDATE social_connection SET variant = 'oauth' WHERE service = 'bluesky' AND variant IS NULL")
    op.execute("UPDATE social_crosspost SET variant = 'oauth' WHERE service = 'bluesky' AND variant IS NULL")

    with op.batch_alter_table('post') as batch_op:
        batch_op.add_column(sa.Column('requires_login', sa.Boolean(), nullable=False, server_default='false'))

    op.create_table(
        'atproto_record',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uri', sa.String(length=512), nullable=False),
        sa.Column('cid', sa.String(length=128), nullable=True),
        sa.Column('did', sa.String(length=255), nullable=False),
        sa.Column('collection', sa.String(length=64), nullable=False),
        sa.Column('rkey', sa.String(length=64), nullable=False),
        sa.Column('direction', sa.String(length=3), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=True),
        sa.Column('post_id', sa.Integer(), nullable=True),
        sa.Column('post_reply_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('vote_kind', sa.String(length=10), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['social_connection.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['post_id'], ['post.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['post_reply_id'], ['post_reply.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('uri'),
    )
    for column in ('did', 'connection_id', 'post_id', 'post_reply_id', 'user_id'):
        op.create_index(f'ix_atproto_record_{column}', 'atproto_record', [column])

    op.create_table(
        'atproto_cursor',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('seq', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('atproto_cursor')
    for column in ('did', 'connection_id', 'post_id', 'post_reply_id', 'user_id'):
        op.drop_index(f'ix_atproto_record_{column}', table_name='atproto_record')
    op.drop_table('atproto_record')
    with op.batch_alter_table('post') as batch_op:
        batch_op.drop_column('requires_login')
    # hosted accounts cannot exist without these columns: they go with them
    op.execute("DELETE FROM social_connection WHERE service = 'bluesky' AND variant = 'hosted'")
    op.execute("UPDATE social_crosspost SET variant = NULL WHERE service = 'bluesky' AND variant = 'oauth'")
    op.execute("UPDATE social_connection SET variant = NULL WHERE service = 'bluesky' AND variant = 'oauth'")
    op.drop_index('uq_social_connection_identity', table_name='social_connection')
    with op.batch_alter_table('social_connection') as batch_op:
        batch_op.drop_column('slot_pinned')
        batch_op.drop_column('slot_state')
        batch_op.drop_column('import_own')
        batch_op.drop_column('is_identity')
