"""split moderation permissions: remove and restore content, and turn comments off, out of moderate all communities

Revision ID: e4c8a1d7f3b6
Revises: d6a1f4c8e2b3
Create Date: 2026-11-05 18:00:00.000000

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, follow-up 4 (owner ruling 2026-11-05). A role holding `moderate
all communities` gains both at the same reach: all domains, or the same domains. Nobody loses anything.

The downgrade drops both, which leaves a role that held them without `moderate all communities` with less, never more.
"""
from alembic import op
from sqlalchemy import text

revision = 'e4c8a1d7f3b6'
down_revision = 'd6a1f4c8e2b3'
branch_labels = None
depends_on = None

SPLIT_FROM = 'moderate all communities'
SPLIT_OUT = ('remove and restore content', 'turn comments off')


def upgrade():
    conn = op.get_bind()
    for permission in SPLIT_OUT:
        # The roles that do not hold it yet, read first: one that already does keeps the reach an admin gave it
        gaining = conn.execute(text("""
            SELECT rp.role_id FROM role_permission rp
            WHERE rp.permission = :source AND NOT EXISTS (
                SELECT 1 FROM role_permission held WHERE held.role_id = rp.role_id AND held.permission = :new)"""),
                               {'source': SPLIT_FROM, 'new': permission}).scalars().all()
        for role_id in gaining:
            conn.execute(text("""
                INSERT INTO role_permission (role_id, permission, all_domains)
                SELECT role_id, :new, all_domains FROM role_permission
                WHERE role_id = :role_id AND permission = :source"""),
                         {'new': permission, 'role_id': role_id, 'source': SPLIT_FROM})
            conn.execute(text("""
                INSERT INTO role_permission_domain (role_id, permission, site_domain_id)
                SELECT role_id, :new, site_domain_id FROM role_permission_domain
                WHERE role_id = :role_id AND permission = :source"""),
                         {'new': permission, 'role_id': role_id, 'source': SPLIT_FROM})


def downgrade():
    conn = op.get_bind()
    for permission in SPLIT_OUT:
        conn.execute(text('DELETE FROM role_permission_domain WHERE permission = :p'), {'p': permission})
        conn.execute(text('DELETE FROM role_permission WHERE permission = :p'), {'p': permission})
