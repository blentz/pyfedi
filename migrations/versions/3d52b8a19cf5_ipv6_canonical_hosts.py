"""IPv6 hosts in one spelling: domain, banned_instances and allowed_instances (owner ruling 2026-10-14)

[0:0::1], 0:0:0:0:0:0:0:1 and ::1 are one address to a browser, and every ban and Domain lookup now compares a host
in app.utils.canonical_host's spelling: ipaddress's compressed form, without brackets. A row saved before that under
another spelling is one no lookup reads -- a ban that bans nothing, a Domain whose posts and blocks are split from
the canonical row's. This rewrites them.

domain: rows whose names are spellings of one IPv6 address are merged into one row named canonically. The survivor
is the row already named canonically if there is one, else the oldest. It takes the sum of post_count, banned,
notify_mods and notify_admins if any row had them, and its own post_warning (with its warning_type) unless it had
none and another row did. Every foreign key referencing domain.id -- read from the catalogue, so a column added
since is not missed: today post, post_reply, scheduled_post and domain_block -- is repointed at the survivor, and
the other rows are deleted. domain_block's key is (user_id, domain_id), so a user's block of a merged row is dropped
first where the same user already blocks the survivor.

banned_instances and allowed_instances (nothing references them): each row is renamed canonically, and a row that
then repeats an older one -- the same host, and for a ban the same subscription_id -- is deleted.

Only names that parse as an IPv6 literal are touched; IPv4, 'host:port' and names stay as they are. A handful of
rows on any instance, updated by primary key, so it is safe on a live database. Memoised ban and block lookups in
the cache expire on their own.

Downgrade is a no-op: the merge discards which spelling each row had, and which rows a merged one was.

Revision ID: 3d52b8a19cf5
Revises: d4a8e2c6f1b9
"""
import ipaddress

import sqlalchemy as sa
from alembic import op

revision = '3d52b8a19cf5'
down_revision = 'd4a8e2c6f1b9'
branch_labels = None
depends_on = None


def canonical(name):
    """app.utils.canonical_host's spelling of an IPv6 literal, or None for anything else. Inlined, not imported, so
    this revision keeps meaning what it meant when it was written."""
    if not name or ':' not in name:
        return None
    literal = name[1:-1] if name.startswith('[') and name.endswith(']') else name
    try:
        return ipaddress.IPv6Address(literal).compressed
    except ValueError:
        return None


def domain_references(connection):
    """(table, column) of every single-column foreign key referencing domain.id."""
    return connection.execute(sa.text(
        "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        "WHERE c.contype = 'f' AND c.confrelid = 'domain'::regclass AND array_length(c.conkey, 1) = 1 "
        "ORDER BY 1, 2")).all()


def merge_domains(connection):
    rows = connection.execute(sa.text(
        "SELECT id, name, post_count, banned, notify_mods, notify_admins, post_warning, warning_type "
        "FROM domain WHERE name LIKE '%:%' ORDER BY id")).mappings().all()
    groups = {}
    for row in rows:
        name = canonical(row['name'])
        if name is not None:
            groups.setdefault(name, []).append(row)
    references = None
    for name, members in groups.items():
        if len(members) == 1 and members[0]['name'] == name:
            continue
        survivor = next((row for row in members if row['name'] == name), members[0])
        others = [row for row in members if row is not survivor]
        warned = next((row for row in [survivor, *others] if row['post_warning']), survivor)
        if references is None:
            references = domain_references(connection)
        for other in others:
            for table, column in references:
                if table == 'domain_block':
                    connection.execute(sa.text(
                        'DELETE FROM domain_block d WHERE d.domain_id = :other AND EXISTS '
                        '(SELECT 1 FROM domain_block s WHERE s.domain_id = :survivor AND s.user_id = d.user_id)'),
                        {'other': other['id'], 'survivor': survivor['id']})
                connection.execute(sa.text(f'UPDATE {table} SET "{column}" = :survivor WHERE "{column}" = :other'),
                                   {'other': other['id'], 'survivor': survivor['id']})
            connection.execute(sa.text('DELETE FROM domain WHERE id = :id'), {'id': other['id']})
        connection.execute(sa.text(
            'UPDATE domain SET name = :name, post_count = :post_count, banned = :banned, notify_mods = :notify_mods, '
            'notify_admins = :notify_admins, post_warning = :post_warning, warning_type = :warning_type '
            'WHERE id = :id'),
            {'id': survivor['id'], 'name': name,
             'post_count': sum(row['post_count'] or 0 for row in members),
             'banned': any(row['banned'] for row in members),
             'notify_mods': any(row['notify_mods'] for row in members),
             'notify_admins': any(row['notify_admins'] for row in members),
             'post_warning': warned['post_warning'], 'warning_type': warned['warning_type']})


def rename_hosts(connection, table, key_columns):
    """Rename table.domain canonically, deleting a row that then repeats an older one on (domain, *key_columns)."""
    extra = ''.join(f', {column}' for column in key_columns)
    rows = connection.execute(sa.text(
        f"SELECT id, domain{extra} FROM {table} WHERE domain LIKE '%:%' ORDER BY id")).mappings().all()
    seen = set()
    for row in rows:
        name = canonical(row['domain'])
        if name is None:
            continue
        key = (name, *(row[column] for column in key_columns))
        if key in seen:
            connection.execute(sa.text(f'DELETE FROM {table} WHERE id = :id'), {'id': row['id']})
            continue
        seen.add(key)
        if row['domain'] != name:
            connection.execute(sa.text(f'UPDATE {table} SET domain = :name WHERE id = :id'),
                               {'id': row['id'], 'name': name})


def upgrade():
    connection = op.get_bind()
    merge_domains(connection)
    rename_hosts(connection, 'banned_instances', ('subscription_id',))
    rename_hosts(connection, 'allowed_instances', ())


def downgrade():
    # Deliberately nothing: see the module docstring. The rows are left in the canonical spelling, which the code
    # before this revision also reads for every host it was given in that spelling.
    pass
