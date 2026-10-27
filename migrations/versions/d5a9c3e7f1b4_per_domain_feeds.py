"""each local feed belongs to a hosted domain, and a feed name is unique per domain

Feeds become per-domain the way communities already are. Each existing local feed is stamped with its owner's home
domain (the default domain when the owner has none or the feed has no owner), so on a site where every feed belongs to
a first-domain account every feed lands on the first domain and its documents do not change. Remote feeds stay NULL.
The unique index on the feed name becomes a plain one, and a unique index on (domain, lower(name)) over local feeds
takes its place.

Creating the unique index aborts the upgrade if two legacy local feeds on one domain differ only by case (the old
unique index held names case-sensitively). Rename one first.

The downgrade cannot always run: recreating the unique `ix_feed_name` fails while two domains share a feed name (or
two names differ only by case). Rename the duplicates first.

Revision ID: d5a9c3e7f1b4
Revises: c6e2a8f4b1d9
Create Date: 2026-10-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5a9c3e7f1b4'
down_revision = 'c6e2a8f4b1d9'
branch_labels = None
depends_on = None

# A local feed moves to its owner's home domain, or to the default domain when the owner has none (or there is no owner).
BACKFILL_FEEDS = ('UPDATE "feed" SET site_domain_id = COALESCE('
                  '(SELECT u.site_domain_id FROM "user" u WHERE u.id = "feed".user_id), '
                  '(SELECT id FROM site_domain WHERE is_default)) WHERE ap_id IS NULL')


def upgrade():
    with op.batch_alter_table('feed', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_domain_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_feed_site_domain_id', 'site_domain', ['site_domain_id'], ['id'])

    op.execute(BACKFILL_FEEDS)

    with op.batch_alter_table('feed', schema=None) as batch_op:
        batch_op.drop_index('ix_feed_name')
        batch_op.create_index('ix_feed_name', ['name'], unique=False)
        batch_op.create_index('ix_feed_domain_lower_name', ['site_domain_id', sa.literal_column('lower(name)')],
                              unique=True, postgresql_where=sa.text('ap_id IS NULL'))


def downgrade():
    """Fails while two domains share a feed name: the unique `ix_feed_name` cannot be recreated over duplicates."""
    with op.batch_alter_table('feed', schema=None) as batch_op:
        batch_op.drop_index('ix_feed_domain_lower_name')
        batch_op.drop_index('ix_feed_name')
        batch_op.create_index('ix_feed_name', ['name'], unique=True)
        batch_op.drop_constraint('fk_feed_site_domain_id', type_='foreignkey')
        batch_op.drop_column('site_domain_id')
