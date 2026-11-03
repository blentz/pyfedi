"""role grants: a role held instance-wide or on one domain, and Admin as the one built-in role

Revision ID: b4e8d2a6f1c9
Revises: d7f2b9c4e1a3
Create Date: 2026-11-03 18:00:00.000000

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, build step 2 (RB1, RB2, RB11, RB16).

- `role_grant` replaces `user_role`: who holds which role, instance-wide (site_domain_id NULL) or on one domain,
  who granted it, when, and an optional expiry. Every `user_role` row becomes an instance-wide grant.
- `role` gains `built_in` (exactly one row: Admin, which holds every permission and is found by this flag, never by
  its name or id), `listed_publicly`, `description` and timestamps. Its name becomes unique, ignoring case. `weight`
  goes: RB6 makes ranking roles pointless.
- The row named Admin (else id 4, else a new row) becomes the built-in role, and its permission rows are deleted,
  since it implies them all. The row named Staff is listed publicly, as the profile badge and /about show it today.
- Roles 1 and 2 ("Anonymous user", "Authenticated user") confer nothing; role 2 was only the admin user form's
  "no role". Their assignments and the rows themselves are deleted, when they hold no permissions.
- `mod_log.site_domain_id`: the domain a scoped action was taken under (RB9).

The downgrade is lossy: domain-scoped grants, expiries and who granted what are dropped, and the deleted roles 1
and 2 and Admin's permission rows are not restored.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4e8d2a6f1c9'
down_revision = 'd7f2b9c4e1a3'
branch_labels = None
depends_on = None

ADMIN_PERMISSIONS = ('approve registrations', 'change user roles', 'ban users', 'manage users',
                     'change instance settings', 'administer all communities', 'administer all users',
                     'edit cms pages')


def upgrade():
    conn = op.get_bind()

    op.create_table(
        'role_grant',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', name='fk_role_grant_user_id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('role_id', sa.Integer(), sa.ForeignKey('role.id', name='fk_role_grant_role_id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('site_domain_id', sa.Integer(),
                  sa.ForeignKey('site_domain.id', name='fk_role_grant_site_domain_id', ondelete='CASCADE'),
                  nullable=True),
        sa.Column('granted_by', sa.Integer(),
                  sa.ForeignKey('user.id', name='fk_role_grant_granted_by', ondelete='SET NULL'), nullable=True),
        sa.Column('granted_at', sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_role_grant_user_id', 'role_grant', ['user_id'])
    op.create_index('ix_role_grant_role_id', 'role_grant', ['role_id'])
    op.execute('CREATE UNIQUE INDEX uq_role_grant_user_role_scope ON role_grant '
               '(user_id, role_id, COALESCE(site_domain_id, 0))')

    with op.batch_alter_table('role') as batch_op:
        batch_op.add_column(sa.Column('built_in', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('listed_publicly', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('description', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True,
                                      server_default=sa.text("timezone('utc', now())")))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True,
                                      server_default=sa.text("timezone('utc', now())")))

    # Admin: by name first (is_admin() matched the name), else the seeded id, else a new row.
    admin_id = conn.execute(sa.text("SELECT id FROM role WHERE name = 'Admin' ORDER BY id LIMIT 1")).scalar()
    if admin_id is None:
        admin_id = conn.execute(sa.text("SELECT id FROM role WHERE id = 4")).scalar()
    if admin_id is None:
        admin_id = conn.execute(sa.text("INSERT INTO role (name) VALUES ('Admin') RETURNING id")).scalar()
    conn.execute(sa.text("UPDATE role SET built_in = true, name = 'Admin' WHERE id = :id"), {'id': admin_id})
    conn.execute(sa.text('DELETE FROM role_permission WHERE role_id = :id'), {'id': admin_id})
    conn.execute(sa.text("UPDATE role SET listed_publicly = true WHERE name = 'Staff'"))

    # Roles 1 and 2 confer nothing: drop their assignments and the rows, unless an instance gave them permissions.
    for role_id in (1, 2):
        if role_id == admin_id:
            continue
        holds = conn.execute(sa.text('SELECT 1 FROM role_permission WHERE role_id = :id LIMIT 1'),
                             {'id': role_id}).scalar()
        if not holds:
            conn.execute(sa.text('DELETE FROM user_role WHERE role_id = :id'), {'id': role_id})
            conn.execute(sa.text('DELETE FROM role WHERE id = :id'), {'id': role_id})

    # Names unique ignoring case: a later duplicate gets the row id appended, so nothing is lost.
    conn.execute(sa.text(
        "UPDATE role r SET name = left(r.name, 40) || ' (' || r.id || ')' "
        "WHERE EXISTS (SELECT 1 FROM role o WHERE lower(o.name) = lower(r.name) AND o.id < r.id) "
        "AND NOT r.built_in"))
    op.execute('CREATE UNIQUE INDEX uq_role_name_lower ON role (lower(name))')
    op.execute('CREATE UNIQUE INDEX uq_role_one_built_in ON role (built_in) WHERE built_in')

    conn.execute(sa.text('INSERT INTO role_grant (user_id, role_id) '
                         'SELECT user_id, role_id FROM user_role ON CONFLICT DO NOTHING'))
    op.drop_table('user_role')

    with op.batch_alter_table('role') as batch_op:
        batch_op.drop_column('weight')

    with op.batch_alter_table('mod_log') as batch_op:
        batch_op.add_column(sa.Column('site_domain_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_mod_log_site_domain_id', 'site_domain', ['site_domain_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade():
    conn = op.get_bind()
    with op.batch_alter_table('mod_log') as batch_op:
        batch_op.drop_constraint('fk_mod_log_site_domain_id', type_='foreignkey')
        batch_op.drop_column('site_domain_id')

    with op.batch_alter_table('role') as batch_op:
        batch_op.add_column(sa.Column('weight', sa.Integer(), nullable=True, server_default='0'))

    op.create_table(
        'user_role',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id')),
        sa.Column('role_id', sa.Integer(), sa.ForeignKey('role.id')),
        sa.PrimaryKeyConstraint('user_id', 'role_id'),
    )
    conn.execute(sa.text('INSERT INTO user_role (user_id, role_id) SELECT DISTINCT user_id, role_id FROM role_grant '
                         'WHERE site_domain_id IS NULL'))
    # Admin's rows again, as the permission checks before this revision read them.
    admin_id = conn.execute(sa.text('SELECT id FROM role WHERE built_in')).scalar()
    if admin_id is not None:
        for permission in ADMIN_PERMISSIONS:
            conn.execute(sa.text('INSERT INTO role_permission (role_id, permission) VALUES (:id, :p) '
                                 'ON CONFLICT DO NOTHING'), {'id': admin_id, 'p': permission})

    op.execute('DROP INDEX IF EXISTS uq_role_one_built_in')
    op.execute('DROP INDEX IF EXISTS uq_role_name_lower')
    with op.batch_alter_table('role') as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')
        batch_op.drop_column('description')
        batch_op.drop_column('listed_publicly')
        batch_op.drop_column('built_in')

    op.drop_table('role_grant')
