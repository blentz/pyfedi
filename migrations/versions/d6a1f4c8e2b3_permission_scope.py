"""permission scope: a role's permissions say which domains they reach; a grant only says who holds the role

Revision ID: d6a1f4c8e2b3
Revises: c5f9e3b7a2d1
Create Date: 2026-11-05 12:00:00.000000

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, owner ruling 2026-11-05.

- `role_permission.all_domains` and `role_permission_domain`: each permission a role holds reaches every domain, or
  those listed. Limited with nothing listed reaches nowhere, so removing a domain can never widen a role.
- `role_grant.site_domain_id` goes. A role granted on a domain becomes a role of its own, "<role> on <domain>", limited
  to that domain and held by the same people, so nobody gains or loses anything.
- The role named Staff gains `administer all users`, and its scopable permissions are limited to the default domain.
  Staff holders lose what they could do on other domains and on remote rows, and gain user administration here.
- `site_domain.registration`: 'staff' and 'admin' merge into 'closed'. It only says whether the public may sign up.

The downgrade is lossy: it restores Staff's reach but drops every other limited permission, rather than let a limited
permission become an instance-wide one. Roles made from domain grants stay as they are, without those permissions.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd6a1f4c8e2b3'
down_revision = 'c5f9e3b7a2d1'
branch_labels = None
depends_on = None

# Frozen here: the catalogue may change after this revision.
STAFF_SCOPABLE = ('approve registrations', 'ban users', 'manage users', 'view user details',
                  'moderate all communities', 'administer communities pages', 'view modlog', 'administer all users')


def upgrade():
    conn = op.get_bind()
    op.add_column('role_permission', sa.Column('all_domains', sa.Boolean(), nullable=False, server_default='true'))
    op.create_table(
        'role_permission_domain',
        sa.Column('role_id', sa.Integer(), primary_key=True),
        sa.Column('permission', sa.String(), primary_key=True),
        sa.Column('site_domain_id', sa.Integer(),
                  sa.ForeignKey('site_domain.id', name='fk_role_permission_domain_site_domain_id', ondelete='CASCADE'),
                  primary_key=True),
        sa.ForeignKeyConstraint(['role_id', 'permission'], ['role_permission.role_id', 'role_permission.permission'],
                                name='fk_role_permission_domain_permission', ondelete='CASCADE'))

    scoped = conn.execute(sa.text(
        'SELECT DISTINCT g.role_id, g.site_domain_id, r.name, r.description, r.listed_publicly, d.domain '
        'FROM role_grant g JOIN role r ON r.id = g.role_id JOIN site_domain d ON d.id = g.site_domain_id')).all()
    for role_id, domain_id, name, description, listed, domain in scoped:
        new_name, n = f'{name} on {domain}'[:50], 1
        while conn.execute(sa.text('SELECT 1 FROM role WHERE lower(name) = lower(:n)'), {'n': new_name}).first():
            n += 1
            new_name = f'{name} on {domain}'[:47] + f' {n}'
        new_id = conn.execute(sa.text(
            'INSERT INTO role (name, description, listed_publicly, built_in) VALUES (:n, :d, :l, false) RETURNING id'),
            {'n': new_name, 'd': description, 'l': listed}).scalar()
        conn.execute(sa.text('INSERT INTO role_permission (role_id, permission, all_domains) '
                             'SELECT :new, permission, false FROM role_permission WHERE role_id = :old'),
                     {'new': new_id, 'old': role_id})
        conn.execute(sa.text('INSERT INTO role_permission_domain (role_id, permission, site_domain_id) '
                             'SELECT :new, permission, :d FROM role_permission WHERE role_id = :new'),
                     {'new': new_id, 'd': domain_id})
        conn.execute(sa.text('UPDATE role_grant SET role_id = :new, site_domain_id = NULL '
                             'WHERE role_id = :old AND site_domain_id = :d'),
                     {'new': new_id, 'old': role_id, 'd': domain_id})

    op.execute('DROP INDEX uq_role_grant_user_role_scope')
    op.drop_constraint('fk_role_grant_site_domain_id', 'role_grant', type_='foreignkey')
    op.drop_column('role_grant', 'site_domain_id')
    op.create_index('uq_role_grant_user_role', 'role_grant', ['user_id', 'role_id'], unique=True)

    staff = conn.execute(sa.text("SELECT id FROM role WHERE name = 'Staff' AND NOT built_in")).scalar()
    default = conn.execute(sa.text('SELECT id FROM site_domain WHERE is_default')).scalar()
    if staff is not None and default is not None:
        conn.execute(sa.text("INSERT INTO role_permission (role_id, permission, all_domains) "
                             "VALUES (:r, 'administer all users', true) ON CONFLICT DO NOTHING"), {'r': staff})
        for permission in STAFF_SCOPABLE:
            held = conn.execute(sa.text('UPDATE role_permission SET all_domains = false WHERE role_id = :r '
                                        'AND permission = :p RETURNING permission'),
                                {'r': staff, 'p': permission}).first()
            if held:
                conn.execute(sa.text('INSERT INTO role_permission_domain (role_id, permission, site_domain_id) '
                                     'VALUES (:r, :p, :d)'), {'r': staff, 'p': permission, 'd': default})

    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.execute("UPDATE site_domain SET registration = 'closed' WHERE registration <> 'anyone'")
    op.create_check_constraint('ck_site_domain_registration', 'site_domain', "registration IN ('anyone','closed')")


def downgrade():
    conn = op.get_bind()
    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.execute("UPDATE site_domain SET registration = 'admin' WHERE registration = 'closed'")
    op.create_check_constraint('ck_site_domain_registration', 'site_domain',
                               "registration IN ('anyone','staff','admin')")

    op.drop_index('uq_role_grant_user_role', table_name='role_grant')
    op.add_column('role_grant', sa.Column('site_domain_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_role_grant_site_domain_id', 'role_grant', 'site_domain', ['site_domain_id'], ['id'],
                          ondelete='CASCADE')
    op.execute('CREATE UNIQUE INDEX uq_role_grant_user_role_scope ON role_grant '
               '(user_id, role_id, COALESCE(site_domain_id, 0))')

    staff = conn.execute(sa.text("SELECT id FROM role WHERE name = 'Staff' AND NOT built_in")).scalar()
    if staff is not None:
        conn.execute(sa.text("DELETE FROM role_permission WHERE role_id = :r AND permission = 'administer all users' "
                             "AND NOT all_domains"), {'r': staff})
        conn.execute(sa.text('UPDATE role_permission SET all_domains = true WHERE role_id = :r'), {'r': staff})
    op.execute('DELETE FROM role_permission WHERE NOT all_domains')   # never let a limited permission widen
    op.drop_table('role_permission_domain')
    op.drop_column('role_permission', 'all_domains')
