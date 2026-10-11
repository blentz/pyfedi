"""Interop C1 item 7 (spec 2026-10-11): `/u/<name>/outbox` lists the user's own posts and comments, newest first,
as the Create activities the community outbox already wraps them in, 50 to a page.

Only what a logged-out viewer could see is listed: public, not deleted, published, and not in a private or
local-only community. Private messages are ChatMessage rows, a separate table, so they can never appear. The gates
are item 6's: a deleted user is 410, a banned, unknown or remote one 404.
"""
from datetime import timedelta

import pytest
from sqlalchemy import event

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import POST_STATUS_REVIEWING, VISIBILITY_FOLLOWERS, VISIBILITY_UNLISTED
from app.models import Community, Instance, Post, User, utcnow
from tests.factories import make_chat_message, make_community, make_instance, make_instance_block, make_post, \
    make_post_reply, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get, signed_ap_get

SERVER = 'https://test.piefed.local'
OUTBOX = f'{SERVER}/u/alice/outbox'


def _world():
    seed_actors()
    return make_user(None, 'alice', local=True), make_community(name='books')


def _post(community, author, minutes_ago, **columns):
    post = make_post(community, author, None)
    post.ap_id = f'{SERVER}/post/{post.id}'
    post.posted_at = utcnow() - timedelta(minutes=minutes_ago)
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _reply(post, author, minutes_ago, **columns):
    reply = make_post_reply(post, author)
    reply.ap_id = f'{SERVER}/comment/{reply.id}'
    reply.posted_at = utcnow() - timedelta(minutes=minutes_ago)
    for name, value in columns.items():
        setattr(reply, name, value)
    db.session.commit()
    return reply


def _hidden_community(flag):
    community = make_community(name=f'hidden_{flag}')
    setattr(community, flag, True)
    db.session.commit()
    return community


def _someone_elses_post(community, deleted=False):
    return _post(community, make_user(None, 'other', local=True), 500, deleted=deleted)


def _object_ids(document):
    return [item['object']['id'] for item in document['orderedItems']]


def test_posts_and_comments_are_listed_newest_first_as_creates(app, db_session):
    author, community = _world()
    old = _post(community, author, 30)
    middle = _reply(old, author, 20)
    new = _post(community, author, 10)

    response = ap_get(app, '/u/alice/outbox')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == OUTBOX
    assert response.json['totalItems'] == 3
    assert _object_ids(response.json) == [new.ap_id, middle.ap_id, old.ap_id]
    items = response.json['orderedItems']
    assert [item['type'] for item in items] == ['Create', 'Create', 'Create']
    assert [item['object']['type'] for item in items] == ['Page', 'Note', 'Page']
    assert {item['actor'] for item in items} == {f'{SERVER}/u/alice'}
    assert {item['audience'] for item in items} == {community.public_url()}
    assert all(item['id'].startswith(f'{SERVER}/activities/create/') for item in items)


def test_the_outbox_is_paged_with_the_first_page_inline(app, db_session, monkeypatch):
    monkeypatch.setattr(activitypub_routes, 'OUTBOX_PAGE_SIZE', 2)
    author, community = _world()
    oldest = _post(community, author, 30)
    middle = _reply(oldest, author, 20)
    newest = _post(community, author, 10)

    root = ap_get(app, '/u/alice/outbox').json
    first = ap_get(app, '/u/alice/outbox?page=1').json
    second = ap_get(app, '/u/alice/outbox?page=2').json

    assert (root['totalItems'], root['first'], root['last']) == (3, f'{OUTBOX}?page=1', f'{OUTBOX}?page=2')
    assert _object_ids(root) == [newest.ap_id, middle.ap_id]
    assert _object_ids(first) == [newest.ap_id, middle.ap_id]
    assert (first['type'], first['partOf'], first['next']) == ('OrderedCollectionPage', OUTBOX, f'{OUTBOX}?page=2')
    assert _object_ids(second) == [oldest.ap_id]
    assert (second['totalItems'], second['prev']) == (3, f'{OUTBOX}?page=1')
    assert 'next' not in second


# Each builder makes one object of alice's that a logged-out viewer may not see, and returns its ap_id.
EXCLUDED = {
    'followers-only post': lambda author, community: _post(community, author, 5, visibility=VISIBILITY_FOLLOWERS).ap_id,
    'unlisted post': lambda author, community: _post(community, author, 5, visibility=VISIBILITY_UNLISTED).ap_id,
    'deleted post': lambda author, community: _post(community, author, 5, deleted=True).ap_id,
    'post under review': lambda author, community: _post(community, author, 5, status=POST_STATUS_REVIEWING).ap_id,
    'post in a private community': lambda author, community: _post(_hidden_community('private'), author, 5).ap_id,
    'post in a local-only community': lambda author, community: _post(_hidden_community('local_only'), author, 5).ap_id,
    'deleted comment': lambda author, community: _reply(_someone_elses_post(community), author, 5, deleted=True).ap_id,
    'followers-only comment': lambda author, community: _reply(_someone_elses_post(community), author, 5,
                                                              visibility=VISIBILITY_FOLLOWERS).ap_id,
    'comment in a private community': lambda author, community: _reply(
        _someone_elses_post(_hidden_community('private')), author, 5).ap_id,
    'comment in a local-only community': lambda author, community: _reply(
        _someone_elses_post(_hidden_community('local_only')), author, 5).ap_id,
    'comment under a deleted post': lambda author, community: _reply(
        _someone_elses_post(community, deleted=True), author, 5).ap_id,
    # Beyond the brief: a logged-out viewer is answered 404 for these too (show_post, the comment page)
    'post in a banned community': lambda author, community: _post(_hidden_community('banned'), author, 5).ap_id,
    'comment in a banned community': lambda author, community: _reply(
        _someone_elses_post(_hidden_community('banned')), author, 5).ap_id,
    'comment under a followers-only post': lambda author, community: _reply(
        _post(community, make_user(None, 'other', local=True), 500, visibility=VISIBILITY_FOLLOWERS), author, 5).ap_id,
    'comment under a post under review': lambda author, community: _reply(
        _post(community, make_user(None, 'other', local=True), 500, status=POST_STATUS_REVIEWING), author, 5).ap_id,
}


@pytest.mark.parametrize('label', EXCLUDED)
def test_what_a_logged_out_viewer_cannot_see_is_not_listed(app, db_session, label):
    author, community = _world()
    shown = _post(community, author, 60)
    hidden = EXCLUDED[label](author, community)

    response = ap_get(app, '/u/alice/outbox')

    assert response.json['totalItems'] == 1
    assert _object_ids(response.json) == [shown.ap_id]
    assert hidden not in response.get_data(as_text=True)


def test_a_private_message_is_never_listed(app, db_session):
    author, community = _world()
    message_id = make_chat_message(author, make_user(None, 'bob', local=True), f'{SERVER}/private_message/1').ap_id

    response = ap_get(app, '/u/alice/outbox')

    assert response.json['totalItems'] == 0
    assert message_id not in response.get_data(as_text=True)


def test_the_lookup_ignores_case_and_answers_the_canonical_id(app, db_session):
    _world()

    assert ap_get(app, '/u/ALICE/outbox').json['id'] == OUTBOX


def test_the_outbox_uses_the_collection_cache_policy(app, db_session):
    _world()

    response = ap_get(app, '/u/alice/outbox')

    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_a_deleted_user_is_gone(app, db_session):
    author, community = _world()
    author.deleted = True
    author.banned = True
    db.session.commit()

    assert ap_get(app, '/u/alice/outbox').status_code == 410


def test_a_banned_user_stays_404(app, db_session):
    author, community = _world()
    author.banned = True
    db.session.commit()

    assert ap_get(app, '/u/alice/outbox').status_code == 404


@pytest.mark.parametrize('path', ['/u/nosuch/outbox', '/u/bob/outbox', '/u/bob@remote.example/outbox'])
def test_an_unknown_or_remote_user_outbox_is_not_served(app, db_session, path):
    _world()
    bob = make_user(make_instance('remote.example'), 'bob')
    _post(make_community(name='elsewhere'), bob, 5)

    assert ap_get(app, path).status_code == 404


def _statements_for(app, path):
    """How many SQL statements one request runs. The session is closed first, so both measurements start from an
    empty identity map and neither is cheaper for objects an earlier request left loaded."""
    db.session.close()
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        response = ap_get(app, path)
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    return len(seen)


def test_a_page_costs_the_same_queries_however_much_the_user_has_written(app, db_session, monkeypatch):
    """The page query is bounded: LIMIT/OFFSET over one union, then one load per kind. Page 1 shows the same two
    objects both times; only the older history behind it grows."""
    monkeypatch.setattr(activitypub_routes, 'OUTBOX_PAGE_SIZE', 2)
    author, community = _world()
    author_id, community_id = author.id, community.id
    base_id = _someone_elses_post(community).id

    def write(count, start):
        author, community = db.session.get(User, author_id), db.session.get(Community, community_id)
        for i in range(count):
            if i % 2:
                _reply(db.session.get(Post, base_id), author, start + i)
            else:
                _post(community, author, start + i)

    write(4, 0)
    _statements_for(app, '/u/alice/outbox?page=1')    # warms the per-process caches (admin ids, settings rows)
    small = _statements_for(app, '/u/alice/outbox?page=1')
    write(20, 100)
    large = _statements_for(app, '/u/alice/outbox?page=1')

    assert small == large


def _blocking(author):
    """alice blocks blocked.example; peer2.example is a known instance she has not blocked."""
    make_instance_block(author, make_instance('blocked.example'))
    make_instance('peer2.example')


def test_an_instance_the_user_blocked_is_refused(app, db_session):
    """D194, as post_ap_refusal and comment_ap: a blocked instance may not read in bulk what it may not fetch one
    object at a time."""
    author, community = _world()
    hidden = _post(community, author, 5)
    _blocking(author)

    response = ap_get(app, '/u/alice/outbox', user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401
    assert b'blocked.example' in response.data
    assert hidden.ap_id.encode() not in response.data


def test_a_blocked_signer_is_refused_whatever_its_user_agent_claims(app, db_session):
    author, community = _world()
    _blocking(author)
    signer = make_user(db.session.query(Instance).filter_by(domain='blocked.example').one(), 'signer', with_keys=True)

    response = signed_ap_get(app, '/u/alice/outbox', signer, 'Test (+https://peer2.example)')

    assert response.status_code == 401


def test_a_user_who_blocks_instances_has_an_outbox_that_varies_on_the_requester(app, db_session):
    """Served to some instances and refused to others, so a shared cache must key on who asks."""
    author, community = _world()
    shown = _post(community, author, 5)
    _blocking(author)

    response = ap_get(app, '/u/alice/outbox', user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert _object_ids(response.json) == [shown.ap_id]
    assert [part.strip() for part in response.headers['Vary'].split(',')][:3] == ['Accept', 'User-Agent', 'Signature']
