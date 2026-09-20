"""The three smallest modules under app/api/alpha/utils.

MEASUREMENT BASIS, from the full-suite --cov=app run at acb3ec132:

    app/api/alpha/utils/domain.py   25.000    9 gaps
    app/api/alpha/utils/topic.py    18.367   40 gaps
    app/api/alpha/utils/upload.py   25.000   33 gaps

Nothing in tests/ referenced any of the three.

No production change. The two shapes in upload.py that looked like defects were
probed and are registered instead: the auth fallback (R1) and the unused quota
sum (R2).

`api_baseline` is the fixture these modules assume -- tests/conftest.py:558 --
because they were written against a seeded dev database.
"""
import pytest
from unittest.mock import patch

from flask_login import login_user

from app import db
from app.api.alpha.utils.domain import post_domain_block
from app.api.alpha.utils.topic import get_topic_list
from app.api.alpha.utils.upload import (post_image_delete, post_upload_community_image,
                                        post_upload_image, post_upload_user_image)
from app.models import Community, Domain, DomainBlock, Topic
from tests.factories import bearer, make_community, make_domain

pytestmark = pytest.mark.usefixtures('site')


# --------------------------------------------------------------------------
# domain.py
# --------------------------------------------------------------------------


def test_blocking_a_domain_through_the_api_records_the_block(app, db_session,
                                                             api_baseline):
    """The `block: True` arm. The return value is the endpoint's whole
    contract, and the row asserts the side effect as well -- a handler that
    returned True without blocking would satisfy the first assertion alone.
    """
    user = api_baseline.user1
    domain = make_domain('blocked.example')

    with app.test_request_context('/'):
        result = post_domain_block(bearer(user), {'domain': 'blocked.example',
                                                  'block': True})

    assert result is True
    assert DomainBlock.query.filter_by(user_id=user.id, domain_id=domain.id).first()


def test_unblocking_a_domain_through_the_api_removes_the_block(app, db_session,
                                                               api_baseline):
    """The `block: False` arm, seeded from the blocked state so the removal is
    what is observed rather than the absence of a block that was never there.
    """
    user = api_baseline.user1
    domain = make_domain('blocked.example')
    db.session.add(DomainBlock(user_id=user.id, domain_id=domain.id))
    db.session.commit()

    with app.test_request_context('/'):
        result = post_domain_block(bearer(user), {'domain': 'blocked.example',
                                                  'block': False})

    assert result is False
    assert DomainBlock.query.filter_by(user_id=user.id, domain_id=domain.id).first() is None


def test_a_domain_block_needs_a_token(app, db_session, api_baseline):
    """The endpoint takes `auth` straight to app/shared/domain.py, which
    refuses an absent one -- there is no anonymous arm.
    """
    make_domain('blocked.example')

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='incorrect_login'):
            post_domain_block(None, {'domain': 'blocked.example', 'block': True})


# --------------------------------------------------------------------------
# topic.py
# --------------------------------------------------------------------------


def _topic(name, parent=None):
    topic = Topic(name=name.title(), machine_name=name, num_communities=0,
                  parent_id=parent.id if parent else None,
                  show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    return topic


def _in_topic(topic, name):
    community = make_community(name)
    community.topic_id = topic.id
    db.session.commit()
    return community


def test_the_topic_list_is_returned_as_a_tree(app, db_session, api_baseline):
    """`process_nested_topics` recurses, and the shape it preserves is the
    point: a flat list would satisfy a test that only counted topics.
    """
    parent = _topic('fediverse')
    child = _topic('microblogging', parent=parent)
    _topic('unrelated')

    with app.test_request_context('/'):
        result = get_topic_list(None, {})

    names = {t['name']: t for t in result['topics']}
    assert set(names) == {'fediverse', 'unrelated'}
    assert [c['name'] for c in names['fediverse']['children']] == ['microblogging']
    assert names['unrelated']['children'] == []


def test_a_leaf_topic_carries_an_empty_children_list(app, db_session, api_baseline):
    """The `else` arm of `if item['children']`. The key must be PRESENT and
    empty rather than absent, or a client walking the tree has to special-case
    leaves.
    """
    _topic('fediverse')

    with app.test_request_context('/'):
        result = get_topic_list(None, {})

    assert result['topics'][0]['children'] == []


def test_the_topic_list_includes_each_topics_communities_by_default(app, db_session,
                                                                    api_baseline):
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')

    with app.test_request_context('/'):
        result = get_topic_list(None, {})

    assert [c['name'] for c in result['topics'][0]['communities']] == ['microblogs']


def test_the_communities_can_be_left_out(app, db_session, api_baseline):
    """`include_communities` defaults True and is read from the request data;
    the false arm is what an index listing asks for.
    """
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')

    with app.test_request_context('/'):
        result = get_topic_list(None, {'include_communities': False})

    assert result['topics'][0]['communities'] == []


def test_an_authorised_reader_gets_the_same_tree(app, db_session, api_baseline):
    """The `if auth:` arm, which resolves a user id and then takes the four
    per-user lookups the anonymous path skips.
    """
    user = api_baseline.user1
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')

    with app.test_request_context('/'):
        result = get_topic_list(bearer(user), {})

    assert [t['name'] for t in result['topics']] == ['fediverse']


def test_a_reader_is_not_shown_a_community_they_blocked(app, db_session, api_baseline):
    """`blocked_community_ids` reaches topic_view, which filters with it. The
    anonymous path passes an empty list, so this is the row that makes the
    logged-in branch's work visible.
    """
    from tests.factories import make_community_block

    user = api_baseline.user1
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')
    blocked = _in_topic(topic, 'blockedcomm')
    make_community_block(user, blocked)
    db.session.commit()

    with app.test_request_context('/'):
        result = get_topic_list(bearer(user), {})

    names = [c['name'] for c in result['topics'][0]['communities']]
    assert 'microblogs' in names
    assert 'blockedcomm' not in names


def test_a_reader_is_not_shown_a_community_on_an_instance_they_blocked(app, db_session,
                                                                       api_baseline):
    """`blocked_instance_ids`, the sibling of the blocked-community row above.

    The two are fetched by different helpers and passed to topic_view as
    different arguments, so covering one says nothing about the other -- the
    mutant emptying this one survived a suite that already covered the
    community list.
    """
    from tests.factories import make_instance, make_instance_block

    user = api_baseline.user1
    peer = make_instance('blocked-peer.example', software='lemmy')
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')
    elsewhere = _in_topic(topic, 'elsewhere')
    elsewhere.instance_id = peer.id
    db.session.commit()
    make_instance_block(user, peer)
    db.session.commit()

    with app.test_request_context('/'):
        result = get_topic_list(bearer(user), {})

    names = [c['name'] for c in result['topics'][0]['communities']]
    assert 'microblogs' in names
    assert 'elsewhere' not in names


def test_an_anonymous_reader_is_passed_empty_filter_lists(app, db_session,
                                                          api_baseline):
    """The `else` arms of both `if user_id:` blocks -- six names set to empty
    lists. Asserted through topic_view's output: with no user there is nothing
    to filter by, so every unblocked community appears.
    """
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')
    _in_topic(topic, 'another')

    with app.test_request_context('/'):
        result = get_topic_list(None, {})

    names = sorted(c['name'] for c in result['topics'][0]['communities'])
    assert names == ['another', 'microblogs']


def test_a_user_id_may_be_supplied_without_a_token(app, db_session, api_baseline):
    """`get_topic_list(auth, data, user_id=...)` -- the parameter exists so an
    already-authenticated caller can skip a second JWT decode. With `auth`
    None and a user_id given, the per-user branch still runs.
    """
    user = api_baseline.user1
    topic = _topic('fediverse')
    _in_topic(topic, 'microblogs')

    with app.test_request_context('/'):
        result = get_topic_list(None, {}, user_id=user.id)

    assert [t['name'] for t in result['topics']] == ['fediverse']


# --------------------------------------------------------------------------
# upload.py
# --------------------------------------------------------------------------


def test_an_image_upload_returns_the_stored_url(app, db_session, api_baseline):
    user = api_baseline.user1

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_upload',
                   return_value='https://cdn.example/a.png') as upload:
            result = post_upload_image(bearer(user), image_file='FILE')

    assert result == {'url': 'https://cdn.example/a.png'}
    assert upload.call_args.args[0] == 'FILE'
    assert upload.call_args.kwargs['user'].id == user.id


@pytest.mark.parametrize('endpoint, destination', [
    (post_upload_community_image, 'communities'),
    (post_upload_user_image, 'users'),
])
def test_the_scoped_uploads_name_their_destination(app, db_session, api_baseline,
                                                   endpoint, destination):
    """R3's two copies. The destination is the only thing separating them, so
    it is what the row asserts.
    """
    user = api_baseline.user1

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_upload',
                   return_value='https://cdn.example/a.png') as upload:
            result = endpoint(bearer(user), image_file='FILE')

    assert result == {'url': 'https://cdn.example/a.png'}
    assert upload.call_args.kwargs['destination'] == destination


@pytest.mark.parametrize('endpoint', [post_upload_community_image, post_upload_user_image])
def test_the_scoped_uploads_refuse_an_unauthorised_caller(app, db_session,
                                                          api_baseline, endpoint):
    """Unlike post_upload_image, these two have NO fallback: authorise_api_user
    is called bare, so a bad token raises whether or not a session exists.
    That asymmetry is the point of the row.
    """
    user = api_baseline.user1

    with app.test_request_context('/'):
        login_user(user)
        with patch('app.api.alpha.utils.upload.process_upload') as upload:
            with pytest.raises(Exception, match='incorrect_login'):
                endpoint('Bearer not-a-real-token', image_file='FILE')

        assert upload.call_args_list == []


def test_an_upload_with_a_bad_token_falls_back_to_the_session(app, db_session,
                                                              api_baseline):
    """R1, pinned as the behaviour it is rather than endorsed.

        PROBE g1 bad token + session  -> {'url': 'u.png'}, as <User user1_1>
        PROBE g2 bad token, no session -> Exception incorrect_login

    A client sending a WRONG token is never told: it is accepted as the session
    user, so it never learns to refresh. Registered rather than changed --
    altering what an invalid token does is an authentication decision.
    """
    user = api_baseline.user1

    with app.test_request_context('/'):
        login_user(user)
        with patch('app.api.alpha.utils.upload.process_upload',
                   return_value='https://cdn.example/a.png') as upload:
            result = post_upload_image('Bearer not-a-real-token', image_file='FILE')

        # Asserted INSIDE the request context. The fallback passes
        # `current_user` itself, which is a LocalProxy: read after the context
        # has exited it resolves to None, and the assertion fails with
        # AttributeError on correct code.
        assert upload.call_args.kwargs['user'].id == user.id

    assert result == {'url': 'https://cdn.example/a.png'}


def test_an_upload_with_a_bad_token_and_no_session_is_refused(app, db_session,
                                                              api_baseline):
    """The other arm of the same fallback, and the one that keeps it from being
    an open door.
    """
    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_upload') as upload:
            with pytest.raises(Exception, match='incorrect_login'):
                post_upload_image('Bearer not-a-real-token', image_file='FILE')

        assert upload.call_args_list == []


def test_the_upload_sums_the_callers_existing_files(app, db_session, api_baseline):
    """R2, pinned so the dead computation is recorded rather than merely
    present: the per-user SELECT over user_file runs on every upload and its
    total is discarded, because the quota check that consumed it is commented
    out. The row asserts the upload still succeeds with rows present, which is
    all the sum can affect today.
    """
    from sqlalchemy import text
    from tests.factories import make_file

    user = api_baseline.user1
    # user_file.file_id is a real FK, so the row needs a File behind it.
    stored = make_file(source_url='https://cdn.example/old.png')
    db.session.execute(
        text('INSERT INTO "user_file" (user_id, file_id, size) VALUES (:u, :f, 4096)'),
        {'u': user.id, 'f': stored.id})
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_upload',
                   return_value='https://cdn.example/a.png'):
            result = post_upload_image(bearer(user), image_file='FILE')

    assert result == {'url': 'https://cdn.example/a.png'}


def test_deleting_an_image_reports_success(app, db_session, api_baseline):
    user = api_baseline.user1

    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_file_delete') as delete:
            result = post_image_delete(bearer(user), {'file': 'https://cdn.example/a.png'})

    assert result == {'result': 'ok'}
    assert delete.call_args.args[0] == 'https://cdn.example/a.png'
    assert delete.call_args.kwargs['user_id'] == user.id


def test_deleting_an_image_with_a_bad_token_falls_back_to_the_session(app, db_session,
                                                                      api_baseline):
    """The same fallback as the upload endpoint, on the delete endpoint. Both
    copies are covered because R1 is about both.
    """
    user = api_baseline.user1

    with app.test_request_context('/'):
        login_user(user)
        with patch('app.api.alpha.utils.upload.process_file_delete') as delete:
            result = post_image_delete('Bearer not-a-real-token',
                                       {'file': 'https://cdn.example/a.png'})

    assert result == {'result': 'ok'}
    assert delete.call_args.kwargs['user_id'] == user.id


def test_deleting_an_image_with_no_credentials_is_refused(app, db_session,
                                                          api_baseline):
    with app.test_request_context('/'):
        with patch('app.api.alpha.utils.upload.process_file_delete') as delete:
            with pytest.raises(Exception, match='incorrect_login'):
                post_image_delete('Bearer not-a-real-token',
                                  {'file': 'https://cdn.example/a.png'})

        assert delete.call_args_list == []
