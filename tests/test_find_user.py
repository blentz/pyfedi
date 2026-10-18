"""`find_user` (app/auth/util.py): the account a typed login identifier names, in one query whatever matches.

It used to try the user name, then the email, then the profile URL, one query each, so an unknown name ran three
queries and a known user name one: a small timing difference that says which names have accounts (gaps G1 review,
Minor 2). One query now does all three, the user name first, then the email, then the profile URL. The matching is
unchanged:

* user name: case-insensitive, local and not deleted;
* email: case-insensitive, local and not deleted;
* profile URL: `<SERVER_URL>/u/<typed>` lowered and compared for equality (a typed '%' or '_' is no wildcard), not
  deleted.
"""
import pytest
from sqlalchemy import event

from app import db
from app.auth.util import find_user
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
def test_every_identifier_costs_one_query(app, people, typed):
    with app.test_request_context('/'):
        _found, count = queries(app, typed)
    assert count == 1


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
