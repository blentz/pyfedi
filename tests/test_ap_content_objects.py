"""tests/test_ap_content_objects.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import POST_STATUS_PUBLISHED, POST_STATUS_REVIEWING
from tests.factories import (make_activitypub_log, make_community, make_instance,
                             make_instance_block, make_post, make_post_reply, make_user)
from tests.test_actor_profiles import seed_actors

AP_ACCEPT = 'application/activity+json'


def ap_get(app, path, user_agent=None):
    """GET an endpoint as a remote ActivityPub client.

    `is_activitypub_request()` (app/activitypub/util.py) is a substring test on
    the Accept header, so a real header is sent rather than the function being
    doubled -- the parse is part of what is under test.

    `user_agent` is separate because `requestor_domain()` (app/utils.py) reads
    it and returns '' unless it contains a '+'. Since `find_instance_id('')`
    returns None and `has_blocked_instance(None)` returns False, the 401
    instance-block branch in `comment_ap` and `post_ap` is UNREACHABLE without
    a '+'-style agent string. A test that omits it measures the wrong branch.
    """
    headers = {'Accept': AP_ACCEPT}
    if user_agent is not None:
        headers['User-Agent'] = user_agent
    with app.test_client() as client:
        return client.get(path, headers=headers)


def browser_get(app, path):
    """GET with no Accept header at all -- the non-ActivityPub branch.

    Four of these endpoints answer this differently: `comment_ap` delegates to
    `continue_discussion`, `post_ap` to `show_post`, `post_ap_context` aborts
    400, and `post_replies_ap` falls off the end of the function entirely.
    """
    with app.test_client() as client:
        return client.get(path)


def seed_local_post(community=None, user=None, title='a post'):
    """A LOCAL post (`ap_id=None`) in a local community, with a local author.

    `ap_id=None` is what makes `Post.is_local()` true (app/models.py), which is
    the branch `post_ap` serves rather than 301-redirecting. `make_post` takes
    `ap_id` as a REQUIRED POSITIONAL, so it cannot be omitted.
    """
    site, instance = seed_actors()
    community = community or make_community(name='books', host='test.piefed.local')
    user = user or make_user(instance, 'poster', local=True)
    post = make_post(community, user, None, title=title)
    db.session.commit()
    return community, user, post


def _double_the_delegates(monkeypatch):
    """Stop the five delegates from running, and record what they were passed.

    All five are imported INTO `app.activitypub.routes` -- `post_to_page`,
    `comment_model_to_json` and `post_replies_for_ap` from
    `app.activitypub.util`, `continue_discussion` and `show_post` from
    `app.post.routes`, and `block_honey_pot` from `app.utils` -- so they are
    patched on that module, following this campaign's binding-site convention.
    `show_post` and `continue_discussion` render templates and must not run.
    """
    calls = {}
    for name, result in (('post_to_page', {'type': 'Page', 'id': 'https://test.piefed.local/post/1'}),
                         ('comment_model_to_json', {'type': 'Note', 'id': 'https://test.piefed.local/comment/1'}),
                         ('post_replies_for_ap', [{'type': 'Note', 'id': 'https://test.piefed.local/comment/1'}])):
        calls[name] = []
        monkeypatch.setattr(activitypub_routes, name,
                            lambda obj, _n=name, _r=result: calls[_n].append(obj) or dict(_r) if isinstance(_r, dict) else calls[_n].append(obj) or list(_r))
    for name in ('continue_discussion', 'show_post'):
        calls[name] = []
        monkeypatch.setattr(activitypub_routes, name,
                            lambda *a, _n=name, **kw: calls[_n].append((a, kw)) or f'HTML:{_n}')
    calls['block_honey_pot'] = []
    monkeypatch.setattr(activitypub_routes, 'block_honey_pot',
                        lambda: calls['block_honey_pot'].append(True))
    return calls


def test_a_comment_is_served_as_activitypub_json(app, db_session, monkeypatch):
    """`comment_ap`'s ordinary path. Unlike `post_ap` it has NO `is_local()`
    check, so it serves ANY reply it can resolve -- including a remote one,
    which is registered rather than pinned here.

    `Cache-Control` is 120, matching `post_ap` and differing from
    `post_replies_ap` and `post_ap_context`, which both use 15.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['type'] == 'Note'
    assert calls['comment_model_to_json'] == [reply]


def test_a_comment_response_sets_its_link_and_vary_headers(app, db_session, monkeypatch):
    """`Link` points at the HTML alternate; `Vary` is `Accept` because this
    author has blocked no instances.

    `Vary` is asserted as `'Accept, Accept-Encoding'`, NOT `'Accept'`:
    Flask-Compress appends `Accept-Encoding` to every response, so the header
    is never bare (harness fact 38). The two-instance-block branch is
    `test_a_comment_from_a_blocking_author_varies_on_user_agent`.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'
    assert response.headers['Link'] == \
        f'<https://test.piefed.local/comment/{reply.id}>; rel="alternate"; type="text/html"'


def test_an_unknown_comment_is_404(app, db_session, monkeypatch):
    """`PostReply.query.get_or_404` fires before any branch, so this is a 404
    for an ActivityPub request and a browser request alike -- unlike
    `post_replies_ap`, whose lookup sits INSIDE its `is_activitypub_request()`
    branch and is therefore never reached by a browser.
    """
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/comment/999999')

    assert response.status_code == 404


def test_a_comment_in_a_local_only_community_is_403(app, db_session, monkeypatch):
    """First disjunct of `if reply.community.local_only or reply.community.private`.

    `private` is set to False explicitly, not left at its column default, so
    dropping the `local_only` disjunct fails THIS test and not its twin.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = True
    community.private = False
    db.session.commit()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 403


def test_a_comment_in_a_private_community_is_403(app, db_session, monkeypatch):
    """Second disjunct. `local_only` is set to False explicitly for the same
    reason its twin sets `private` explicitly.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = True
    db.session.commit()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 403


def test_a_comment_is_401_when_the_author_has_blocked_the_requesting_instance(app, db_session, monkeypatch):
    """The 401 branch, and it is UNREACHABLE without a '+'-style User-Agent.

    `requestor_domain()` (app/utils.py) returns '' unless the agent string
    contains a '+', `find_instance_id('')` returns None, and
    `has_blocked_instance(None)` returns False. So this test sends
    'Test (+https://blocked.example)', from which `requestor_domain()` extracts
    'blocked.example' -- and the Instance row must already exist with that
    domain, or `find_instance_id` would CREATE one (and return an id the
    author has not blocked).
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    blocked = make_instance('blocked.example')
    make_instance_block(author, blocked)
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401
    assert b'blocked.example' in response.data


def test_a_comment_is_served_when_the_author_blocked_a_different_instance(app, db_session, monkeypatch):
    """The block is per-instance, not a global flag. The author blocks
    'other.example' and the request arrives from 'peer.example', so the 401
    must NOT fire -- this is what stops the guard being satisfied by any block
    at all, and it is a different assertion from the `has_blocked_instances()`
    Vary branch below, which IS a global flag.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200


def test_a_comment_from_a_blocking_author_varies_on_user_agent(app, db_session, monkeypatch):
    """`if reply.author.has_blocked_instances():` -- a GLOBAL "does this author
    block anyone at all" flag, distinct from the per-instance
    `has_blocked_instance(id)` the 401 uses. The response varies on User-Agent
    because the body now depends on who is asking.

    The author blocks 'other.example' while the request comes from
    'peer2.example', so the 401 does NOT fire and this test reaches the header
    -- which is exactly what makes it discriminate the two different methods.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, User-Agent, Accept-Encoding'


def test_a_browser_request_for_a_comment_delegates_to_the_discussion_view(app, db_session, monkeypatch):
    """`comment_ap`'s else branch calls `continue_discussion(reply.post.id,
    comment_id)`. It is asserted to receive the POST's id and the COMMENT's id,
    in that order -- the two are different rows and swapping them is a real
    regression this assertion catches.

    `post_replies_ap` has NO else branch at all and crashes here; that is
    pinned by `test_a_browser_request_for_post_replies_crashes` and fixed in a
    later task.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = browser_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert calls['continue_discussion'] == [((post.id, reply.id), {})]
