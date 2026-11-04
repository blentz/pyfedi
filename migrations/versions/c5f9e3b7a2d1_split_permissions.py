"""split permissions: each role keeps what it can do today, under the catalogue's finer permissions

Revision ID: c5f9e3b7a2d1
Revises: b4e8d2a6f1c9
Create Date: 2026-11-03 21:00:00.000000

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, build step 4 (note 1, RB2, RB5).

The catalogue splits today's eight permissions so a custom role can be composed from finer ones. Every role is given
each permission split from one it holds, so that nothing it may do today is lost:

- `administer all communities` guarded the /admin community, topic, blocked-image, media, vote and remote-instance
  pages, and every moderation check outside /admin. Its rows become all seven permissions split from it. The name
  stays in the catalogue for what only admins may do in a community, which no role held before (note 4).
- `change instance settings` is kept, and gains the parts split from it: domains, masquerade, the newsletter,
  remote instances and domain branding.
- `administer all users` gains `manage media` and `read private messages` (the API's private message reports).
- `ban users` and `manage users` gain `manage blocklists` (link-domain and tag bans).
- `change user roles` is removed: roles and grants are Admin's alone (RB5).
- The role named Staff passed every `is_staff()` check by its name. It gains the permissions those checks become, so
  that once they are converted nothing refers to Staff by name (RB16). This is the last time anything does.

The built-in Admin holds every permission without rows, and is left alone. The downgrade drops the new permissions
and gives `administer all communities` back to every role holding `administer communities pages`, which only it
was ever split into. It is lossy: `change user roles` is not restored.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5f9e3b7a2d1'
down_revision = 'b4e8d2a6f1c9'
branch_labels = None
depends_on = None

LEGACY = ('administer all communities', 'administer all users', 'approve registrations', 'ban users',
          'change instance settings', 'change user roles', 'edit cms pages', 'manage users')

SPLIT_FROM_COMMUNITIES = ('moderate all communities', 'manage topics', 'manage blocklists', 'view vote analytics',
                          'manage media', 'manage remote instances', 'administer communities pages')

# legacy permission held -> the permissions a role holding it gains
GAINS = {
    'administer all communities': SPLIT_FROM_COMMUNITIES,
    'change instance settings': ('manage domains', 'masquerade', 'send newsletter', 'manage remote instances',
                                 'edit domain branding'),
    'administer all users': ('manage media', 'read private messages'),
    'ban users': ('manage blocklists',),
    'manage users': ('manage blocklists',),
}

REMOVED = ('administer all communities', 'change user roles')

# what `is_staff()` let the role named Staff do, as permissions
STAFF_GAINS = ('manage blocklists', 'add remote communities', 'exempt from upload limits', 'generate referral codes',
               'view user details', 'moderate all communities', 'view modlog')
STAFF_NAME = 'Staff'


def upgrade():
    conn = op.get_bind()
    roles = conn.execute(sa.text('SELECT id, name FROM role WHERE NOT built_in')).all()
    for role_id, name in roles:
        held = set(conn.execute(sa.text('SELECT permission FROM role_permission WHERE role_id = :r'),
                                {'r': role_id}).scalars())
        wanted = set(held)
        for permission in held:
            wanted.update(GAINS.get(permission, ()))
        if name == STAFF_NAME:
            wanted.update(STAFF_GAINS)
        wanted.difference_update(REMOVED)
        for permission in sorted(held - wanted):
            conn.execute(sa.text('DELETE FROM role_permission WHERE role_id = :r AND permission = :p'),
                         {'r': role_id, 'p': permission})
        for permission in sorted(wanted - held):
            conn.execute(sa.text('INSERT INTO role_permission (role_id, permission) VALUES (:r, :p)'),
                         {'r': role_id, 'p': permission})


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text(
        'INSERT INTO role_permission (role_id, permission) '
        "SELECT DISTINCT rp.role_id, 'administer all communities' FROM role_permission rp "
        "JOIN role r ON r.id = rp.role_id WHERE NOT r.built_in AND rp.permission = 'administer communities pages' "
        "AND NOT EXISTS (SELECT 1 FROM role_permission x WHERE x.role_id = rp.role_id "
        "                AND x.permission = 'administer all communities')"))
    conn.execute(sa.text('DELETE FROM role_permission WHERE permission NOT IN :legacy').bindparams(
        sa.bindparam('legacy', expanding=True)), {'legacy': list(LEGACY)})
