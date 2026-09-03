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


def test_a_local_post_is_served_as_activitypub_json(app, db_session, monkeypatch):
    """`post_ap`'s ordinary path: a GET with an ActivityPub Accept header for a
    LOCAL post. `post_to_page` is doubled and the route adds `@context` to what
    it returns, so the assertion covers both the delegation and the wrapping.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['type'] == 'Page'
    assert '@context' in response.json
    assert calls['post_to_page'] == [post]


def test_a_post_without_a_slug_links_to_its_numeric_url(app, db_session, monkeypatch):
    """The `else` half of `if post.slug:`. `make_post` never sets `slug`, so
    this is the factory's state -- and it is asserted rather than assumed
    because its twin below sets one explicitly and gets a different URL.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Link'] == \
        f'<https://test.piefed.local/post/{post.id}>; rel="alternate"; type="text/html"'


def test_a_post_with_a_slug_links_to_its_slug_url(app, db_session, monkeypatch):
    """The truthy half. The route interpolates the slug DIRECTLY after the host
    with no separator, so a slug must begin with '/' to produce a valid URL --
    which is itself worth knowing and is asserted here rather than papered over.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.slug = '/c/books/p/1/a-post'
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Link'] == \
        '<https://test.piefed.local/c/books/p/1/a-post>; rel="alternate"; type="text/html"'


def test_a_post_from_a_non_blocking_author_varies_on_accept_only(app, db_session, monkeypatch):
    """`Vary` is `Accept` plus Flask-Compress's `Accept-Encoding`. The author
    blocks nobody, which is `make_user`'s state and is made contrary by the
    twin below rather than being asserted bare.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_a_post_from_a_blocking_author_varies_on_user_agent(app, db_session, monkeypatch):
    """`if post.author.has_blocked_instances():` -- the global flag. The author
    blocks 'other.example' and the request arrives from 'peer2.example', so the
    401 does not fire and the header branch is reached.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')

    response = ap_get(app, f'/post/{post.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, User-Agent, Accept-Encoding'


def test_an_unknown_post_is_404_for_an_activitypub_request(app, db_session, monkeypatch):
    """`Post.query.get_or_404` sits INSIDE the `is_activitypub_request()`
    branch, so this 404 is reached only for an ActivityPub request. A browser
    request for the same id goes to `show_post` instead, which is doubled.
    """
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/post/999999')

    assert response.status_code == 404


def test_a_post_in_a_local_only_community_is_403(app, db_session, monkeypatch):
    """First disjunct of three. `private` and `status` are both set explicitly
    to their non-triggering values, so dropping `local_only` fails THIS test
    alone.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = True
    community.private = False
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403


def test_a_post_in_a_private_community_is_403(app, db_session, monkeypatch):
    """Second disjunct."""
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = True
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403


def test_an_unpublished_post_is_403(app, db_session, monkeypatch):
    """Third disjunct: `post.status < POST_STATUS_PUBLISHED`.

    `POST_STATUS_REVIEWING` is 0 and `POST_STATUS_PUBLISHED` is 1
    (app/constants.py), and `make_post` leaves `status` at the column default,
    which IS `POST_STATUS_PUBLISHED` -- so this test must set it, and both
    community flags are set to False so the other two disjuncts cannot be what
    produced the 403.

    Note `post_replies_ap` and `post_ap_context` apply NO status guard, so the
    same under-review post's replies remain enumerable. Registered, not fixed.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = False
    post.status = POST_STATUS_REVIEWING
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403


def test_a_post_is_401_when_the_author_has_blocked_the_requesting_instance(app, db_session, monkeypatch):
    """`post_ap`'s copy of `comment_ap`'s 401 guard. The Instance row is created
    with the exact domain `requestor_domain()` will extract, because
    `find_instance_id` CREATES AND COMMITS a sparse Instance row for an unknown
    domain -- so an absent row would silently yield an id the author has not
    blocked and this test would measure the wrong branch.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    blocked = make_instance('blocked.example')
    make_instance_block(author, blocked)

    response = ap_get(app, f'/post/{post.id}',
                      user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401


def test_a_remote_post_redirects_to_its_origin(app, db_session, monkeypatch):
    """`post_ap`'s `else` on `if post.is_local():` -- a 301 to the post's own
    `ap_id`. `Post.is_local()` is `ap_id is None or
    ap_id.startswith(SERVER_URL)` (app/models.py), so a remote `ap_id` on a
    DIFFERENT host is what makes it false.

    This is an asymmetry, not just a branch: `comment_ap` has no `is_local()`
    check at all and re-serves a remote reply's JSON as though this instance
    were authoritative for it. Registered, not fixed.
    """
    _double_the_delegates(monkeypatch)
    site, instance = seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    author = make_user(instance, 'remoteposter')
    post = make_post(community, author, 'https://peer.example/objects/xyz')
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 301
    assert response.headers['Location'] == 'https://peer.example/objects/xyz'


def test_a_browser_request_for_a_post_delegates_to_show_post(app, db_session, monkeypatch):
    """`post_ap`'s outer else: `block_honey_pot()` then `show_post(...)`.

    Both are asserted. `block_honey_pot` running is not incidental -- it is a
    side effect on the non-ActivityPub path that the ActivityPub path does not
    have, and no other endpoint in this slice calls it.

    `sort` comes from `current_user.default_comment_sort or 'hot'` for a logged
    -in user and 'hot' for an anonymous one; the test client is anonymous, so
    'hot' is the value asserted.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = browser_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert calls['block_honey_pot'] == [True]
    assert len(calls['show_post']) == 1
    args, kwargs = calls['show_post'][0]
    assert args == (post.id,)
    assert kwargs['sort'] == 'hot'
    assert kwargs['low_bandwidth'] is False


def test_a_post_request_to_a_post_url_never_takes_the_activitypub_path(app, db_session, monkeypatch):
    """`post_ap`'s route accepts POST -- `methods=['GET', 'HEAD', 'POST']` --
    but its ActivityPub branch requires GET or HEAD, so a POST with an
    ActivityPub Accept header falls to `show_post` regardless of the header.

    `post_ap` is the ONLY one of the four content-object endpoints whose route
    accepts POST; the other three are GET-only (`comment_ap` also allows HEAD).
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    with app.test_client() as client:
        response = client.post(f'/post/{post.id}', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert len(calls['show_post']) == 1
    assert calls['post_to_page'] == []


def test_a_head_request_for_a_post_returns_an_empty_activitypub_body(app, db_session, monkeypatch):
    """`post_ap`'s `else: post_data = []` for HEAD -- an empty LIST, jsonified,
    not an empty body. `post_to_page` is NOT called, which is the observable
    difference and is asserted rather than inferred from the body.

    This branch is REACHABLE here because `post_ap`'s route lists HEAD.
    `post_replies_ap` and `post_ap_context` contain the same branch on
    GET-only routes, where it is dead code -- registered, not fixed.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    with app.test_client() as client:
        response = client.head(f'/post/{post.id}', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert calls['post_to_page'] == []
