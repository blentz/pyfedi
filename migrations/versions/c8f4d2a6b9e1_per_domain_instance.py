"""each hosted domain has its own instance row; the default keeps row 1 (spec 2026-10-19 §3.1, plan C2)

Revision ID: c8f4d2a6b9e1
Revises: b7e3c1a9d5f2
Create Date: 2026-10-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8f4d2a6b9e1'
down_revision = 'b7e3c1a9d5f2'
branch_labels = None
depends_on = None

# The default domain keeps row 1. Another domain takes the instance row already named for it (find_instance_id may
# have made a sparse one for the host; the unique domain index allows no second), else gets a new one, and so does the
# default on a database migrated before `flask init-db` has made row 1. A row other than 1 is then
# PieFed's and has no inbox: nothing is ever delivered to a domain of our own. The columns the model defaults in Python
# are written out, as the model would.
NEEDS_ITS_OWN = "(NOT d.is_default OR NOT EXISTS (SELECT 1 FROM instance WHERE id = 1))"
MAKE_INSTANCES = f'''
    INSERT INTO instance (domain, software, created_at, updated_at, last_seen, failures, dormant, gone_forever,
                          trusted, silenced, popular)
    SELECT d.domain, 'PieFed', now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc', 0,
           false, false, false, false, true
    FROM site_domain d
    WHERE {NEEDS_ITS_OWN} AND NOT EXISTS (SELECT 1 FROM instance i WHERE lower(i.domain) = lower(d.domain))
'''
LINK_DOMAINS = f'''
    UPDATE site_domain d SET instance_id = CASE WHEN {NEEDS_ITS_OWN} THEN
        (SELECT i.id FROM instance i WHERE lower(i.domain) = lower(d.domain) ORDER BY i.id LIMIT 1) ELSE 1 END
'''
CLEAR_INBOXES = '''
    UPDATE instance i SET software = 'PieFed', inbox = NULL, shared_inbox = NULL
    FROM site_domain d WHERE d.instance_id = i.id AND i.id <> 1
'''
# Local accounts and communities move to their domain's row, and posts and replies to their local author's.
# The default domain's rows stay on row 1, and remote rows (no site_domain_id) are not touched.
MOVE_OWNERS = '''
    UPDATE "{table}" o SET instance_id = d.instance_id
    FROM site_domain d WHERE o.site_domain_id = d.id AND o.ap_id IS NULL AND NOT d.is_default
'''
MOVE_CONTENT = '''
    UPDATE "{table}" c SET instance_id = d.instance_id
    FROM "user" u JOIN site_domain d ON u.site_domain_id = d.id
    WHERE c.user_id = u.id AND u.ap_id IS NULL AND NOT d.is_default
'''
# Back on row 1, as before C2. The sister domains' instance rows are left: other rows may name them by then, and an
# upgrade run again takes each one back by its domain.
RESTORE = '''
    UPDATE "{table}" SET instance_id = 1
    WHERE instance_id IN (SELECT instance_id FROM site_domain WHERE NOT is_default)
'''


def upgrade():
    with op.batch_alter_table('site_domain', schema=None) as batch_op:
        batch_op.add_column(sa.Column('instance_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_site_domain_instance_id', 'instance', ['instance_id'], ['id'])

    op.execute(MAKE_INSTANCES)
    op.execute(LINK_DOMAINS)
    op.execute(CLEAR_INBOXES)
    for table in ('user', 'community'):
        op.execute(MOVE_OWNERS.format(table=table))
    for table in ('post', 'post_reply'):
        op.execute(MOVE_CONTENT.format(table=table))

    with op.batch_alter_table('site_domain', schema=None) as batch_op:
        batch_op.alter_column('instance_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint('uq_site_domain_instance_id', ['instance_id'])


def downgrade():
    for table in ('post_reply', 'post', 'community', 'user'):
        op.execute(RESTORE.format(table=table))
    with op.batch_alter_table('site_domain', schema=None) as batch_op:
        batch_op.drop_constraint('uq_site_domain_instance_id', type_='unique')
        batch_op.drop_constraint('fk_site_domain_instance_id', type_='foreignkey')
        batch_op.drop_column('instance_id')
