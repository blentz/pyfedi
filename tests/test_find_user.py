"""`find_user` (app/auth/util.py): the account a typed login identifier names, in the same three queries whatever matches.

It used to try the user name, then the email, then the profile URL, stopping at the first hit, so an unknown name ran
three queries and a known user name one: a small timing difference that says which names have accounts (gaps G1
review, Minor 2). A single OR of the three fixed that but could not use any of their indexes, so it scans "user" on
every login and mention. Now all three indexed lookups always run, and the winner is the user name, then the email,
then the profile URL; each uses its own lower() expression index. The matching is unchanged:

* user name: case-insensitive, local and not deleted;
* email: case-insensitive, local and not deleted;
* profile URL: `<SERVER_URL>/u/<typed>` lowered and compared for equality (a typed '%' or '_' is no wildcard), not
  deleted.
"""
import pytest
from sqlalchemy import event

from app import db
from app.auth.util import find_user, find_user_lookups
from tests.factories import make_instance, make_user


@pytest.fixture
def people(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    remote = make_instance('remote.example', software='piefed')
    make_user(local, 'founder', local=True)
    carol = make_user(local, 'Carol', local=True)
    carol.email = 'Carol@Example.com'
    # dave's email is carol's user name: the user name wins
    dave = make_user(local, 'dave', local=True)
    dave.email = 'carol'
    gone = make_user(local, 'gone', local=True)
    gone.email = 'gone@example.com'
    gone.deleted = True
    stranger = make_user(remote, 'stranger', local=False)
    stranger.email = 'stranger@example.com'
    db.session.commit()
    return {'carol': carol, 'dave': dave, 'gone': gone, 'stranger': stranger}


def queries(app, typed):
    db.session.expire_all()
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if 'FROM site_domain' not in statement:   # the domain list is cached in production, and not what is counted
            seen.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        found = find_user(typed)
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    return found, len(seen)


@pytest.mark.parametrize('typed', ['carol', 'carol@example.com', 'https://test.piefed.local/u/carol', 'ghost',
                                   'ghost@example.com'],
                         ids=['name', 'email', 'a-typed-url', 'unknown-name', 'unknown-email'])
def test_every_identifier_costs_three_queries(app, people, typed):
    with app.test_request_context('/'):
        _found, count = queries(app, typed)
    assert count == 3


@pytest.mark.parametrize('typed, who', [
    ('carol', 'carol'), ('CAROL ', 'carol'),
    ('carol@example.com', 'carol'), ('CAROL@EXAMPLE.COM', 'carol'),
    ('dave', 'dave'),
    ('gone', None), ('gone@example.com', None),
    ('stranger', None), ('stranger@example.com', None),
    ('%', None), ('c_rol', None), ('ghost', None),
])
def test_the_matching_is_unchanged(app, people, typed, who):
    with app.test_request_context('/'):
        found = find_user(typed)
    assert found is (people[who] if who else None)


def test_a_user_name_wins_over_an_email_that_is_the_same_text(app, people):
    with app.test_request_context('/'):
        assert find_user('carol') is people['carol']   # not dave, whose email is 'carol'


def test_the_profile_url_is_matched_whole_and_case_insensitively(app, people):
    people['carol'].ap_profile_id = 'https://test.piefed.local/u/carol'
    people['dave'].email = 'dave@example.com'   # or 'carol' would find dave by email first
    db.session.commit()
    with app.test_request_context('/'):
        assert find_user('https://test.piefed.local/u/carol') is None   # typed is appended after /u/
        assert find_user('Carol') is people['carol']
    people['carol'].user_name = 'renamed'
    db.session.commit()
    with app.test_request_context('/'):
        assert find_user('CAROL') is people['carol']   # now through the profile URL alone
        assert find_user('CARO') is None


BULK_USERS = """
    INSERT INTO "user" (user_name, email, instance_id, verified, banned, deleted, searchable, indexable, hide_nsfw,
                        hide_nsfl, created, last_seen, ap_fetched_at, ap_profile_id, password_hash, alt_user_name)
    SELECT 'bulk' || g, 'bulk' || g || '@example.com', :instance_id, true, false, false, true, true, 1, 1, now(),
           now(), now(), 'https://bulk.example/u/' || g, 'x', 'bulk-alt' || g
    FROM generate_series(1, 2000) g"""


@pytest.mark.parametrize('which, index', [('name', 'idx_user_user_name_lower'), ('email', 'idx_user_email_lower'),
                                          ('profile-url', 'idx_user_ap_profile_id_lower')])
def test_each_lookup_uses_its_index_and_does_not_scan_the_table(app, people, which, index):
    """Postgres can use an expression index only for the same expression: lower(user_name), lower(email) and
    lower(ap_profile_id) each have one.

    The plan must not depend on what earlier tests left in "user". A table of five rows is scanned anyway, and with
    sequential scans off every index on it is a full-index-scan substitute of almost the same cost, so which one the
    planner took (once ix_user_cover_id, after an autovacuum had reset the table's statistics) was a coin toss. So the
    transaction gives the table a few thousand rows and ANALYZEs it: an equality probe on the right expression index
    is then far cheaper than reading any other index end to end, whatever state the table was left in. All of it is
    rolled back."""
    position = {'name': 0, 'email': 1, 'profile-url': 2}[which]
    with app.test_request_context('/'):
        statement = find_user_lookups('carol')[position].statement
        compiled = statement.compile(dialect=db.engine.dialect, compile_kwargs={'literal_binds': True})
    try:
        db.session.execute(db.text(BULK_USERS), {'instance_id': people['carol'].instance_id})
        db.session.execute(db.text('ANALYZE "user"'))
        db.session.execute(db.text('SET LOCAL enable_seqscan = off'))
        plan = '\n'.join(row[0] for row in db.session.execute(db.text(f'EXPLAIN {compiled}')))
    finally:
        db.session.rollback()
    assert 'Seq Scan on "user"' not in plan, plan
    assert f'Index Scan using {index} on "user"' in plan or f'Bitmap Index Scan on {index}' in plan, plan
