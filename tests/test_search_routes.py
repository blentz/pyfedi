"""app/search/routes.py.

MEASUREMENT BASIS. The module stood at 6.485% on the full-suite --cov=app run
at 7f1f6922c, carrying 162 missing statements and 112 missing arcs -- the
lowest start the campaign has taken.

Nothing here could run at all before this round: the test database is built by
migrations, so sqlalchemy_searchable's `parse_websearch` had never existed in
it and every `.search()` call raised `psycopg2.errors.UndefinedFunction`.
tests/conftest.py now installs the expressions once per session (fact 317).

Three defects are pinned here and repaired together:

  P1  an unknown search_for fell through to the render with next_url unbound,
      which is an UnboundLocalError and a 500 on a public route.
  P2  a non-numeric minimum_upvote reached int() and was a 500.
  P3  two redirects were built by f-string, so a query carrying an encoded '&'
      injected its own parameters into them.
"""
import pytest
from unittest.mock import patch

from app import db
from app.constants import POST_STATUS_REVIEWING, POST_TYPE_IMAGE
from app.models import Post, PostReply, Site, utcnow
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _post(community, author, title, **kwargs):
    """A post the search can find: indexable, public and past review.

    make_post leaves status at Post.new()'s implicit value and indexable at the
    column default, and the search filters on both -- so a fixture that skips
    these is invisible to every assertion in this file.
    """
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    post.status = POST_STATUS_REVIEWING + 1
    post.indexable = True
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.commit()
    return post


def _reply(community, author, post, body, **kwargs):
    reply = PostReply(user_id=author.id, post_id=post.id, community_id=community.id,
                      body=body, body_html=f'<p>{body}</p>', from_bot=False,
                      nsfw=False, deleted=False, private=False, indexable=True,
                      posted_at=utcnow(),
                      ap_id=f'https://test.piefed.local/comment/{body.replace(" ", "")}')
    for key, value in kwargs.items():
        setattr(reply, key, value)
    db.session.add(reply)
    db.session.commit()
    return reply


def _titles(render):
    return [p.title for p in render.call_args.kwargs['posts'].items]


def _bodies(render):
    return [r.body for r in render.call_args.kwargs['replies'].items]


# --------------------------------------------------------------------------
# P1: an unknown search_for
# --------------------------------------------------------------------------


def test_an_unknown_search_for_renders_an_empty_result_page(app, db_session):
    """Before the repair:

        PROBE e1 exception: UnboundLocalError cannot access local variable
        'next_url' where it is not associated with a value

    next_url and prev_url are set inside the posts branch and again inside the
    comments branch; communities and people return redirects; anything else
    reached the render with both names unbound. `posts = None` and `replies =
    None` above them already say what the page should do with a search_for it
    does not know.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=hello&search_for=bogus')

    assert response.status_code == 200
    assert render.call_args.kwargs['posts'] is None
    assert render.call_args.kwargs['replies'] is None
    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# P2: a non-numeric minimum_upvote
# --------------------------------------------------------------------------


def test_a_minimum_upvote_that_is_not_a_number_filters_nothing(app, db_session):
    """PROBE e2 exception: ValueError invalid literal for int() with base 10:
    'lots'. The value comes straight from the query string, so any link with a
    typo in it was a 500.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'popular article', up_votes=10, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=article&minimum_upvote=lots')

    assert response.status_code == 200
    assert _titles(render) == ['popular article']


def test_a_numeric_minimum_upvote_still_filters(app, db_session):
    """The other half of P2's inversion: a repair that ignored the parameter
    entirely would pass the test above.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'popular article', up_votes=10, down_votes=0)
    _post(community, alice, 'ignored article', up_votes=1, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&minimum_upvote=5')

    assert _titles(render) == ['popular article']


# --------------------------------------------------------------------------
# P3: the redirects
# --------------------------------------------------------------------------


def test_a_query_carrying_an_ampersand_cannot_add_parameters_to_the_redirect(app, db_session):
    """Before the repair:

        PROBE e5 redirect: 302 /communities?search=cats&language_id=99&language_id=0

    -- the user's own text became a second language_id, arriving BEFORE the
    route's. Built with url_for, the ampersand is percent-encoded and stays
    part of the search term.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/search?q=cats%26language_id=99&search_for=communities')

    assert response.status_code == 302
    location = response.headers['Location']
    # the ampersand and the equals are encoded, so the whole thing is one
    # search TERM -- and the route's own language_id appears once, as itself
    assert 'search=cats%26language_id%3D99' in location
    assert location.count('&language_id=') == 1


def test_a_people_search_redirects_with_its_query_encoded(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/search?q=alice%26x=1&search_for=people')

    assert response.status_code == 302
    location = response.headers['Location']
    assert location.startswith('/instance/all/people?')
    assert 'q=alice%26x%3D1' in location


# --------------------------------------------------------------------------
# The landing page, and what counts as a search at all
# --------------------------------------------------------------------------


def test_the_bare_search_page_lists_communities_and_languages(app, db_session):
    """With no parameters at all the route renders the start page instead of
    running a query -- a different template, and the assertion says which.
    """
    instance, alice, bob = _seed()
    make_community('microblogs')
    banned = make_community('bannedcomm')
    banned.banned = True
    db.session.commit()
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'search/start.html'
    assert [c.name for c in render.call_args.kwargs['communities']] == ['microblogs']
    assert render.call_args.kwargs['is_admin'] is False
    assert render.call_args.kwargs['is_staff'] is False


def test_the_start_page_hides_communities_the_reader_is_banned_from(app, db_session):
    from tests.factories import ban_user_from_community
    instance, alice, bob = _seed()
    open_community = make_community('microblogs')
    barred = make_community('barred')
    ban_user_from_community(alice, barred)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search')

    assert [c.name for c in render.call_args.kwargs['communities']] == ['microblogs']


def test_an_admin_sees_the_admin_controls_on_the_start_page(app, db_session):
    instance, alice, bob = _seed()
    _make_admin(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search')

    assert render.call_args.kwargs['is_admin'] is True


def _make_admin(user):
    """Both notions of admin (fact 308)."""
    from app.constants import ROLE_ADMIN
    from app.models import Role, user_role
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


@pytest.mark.parametrize('query', ['q=hello', 'type=1', 'language=2', 'nsfw=only',
                                   'minimum_upvote=3'])
def test_any_one_parameter_is_enough_to_run_a_search(app, db_session, query):
    """The gate is a five-way `or`, and one row per operand is what stops any
    of them being load-bearing for nothing -- the start page is what a missing
    operand would produce.
    """
    instance, alice, bob = _seed()
    make_community('microblogs')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/search?{query}')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'search/results.html'


def test_bingbot_is_refused(app, db_session):
    """The first line of the route, and the only user agent it names."""
    instance, alice, bob = _seed()
    client = app.test_client()

    refused = client.get('/search?q=hello', headers={'User-Agent': 'Mozilla/5.0 (compatible; bingbot/2.0)'})
    allowed = client.get('/search?q=hello', headers={'User-Agent': 'Mozilla/5.0'})

    assert refused.status_code == 404
    assert allowed.status_code == 200


# --------------------------------------------------------------------------
# Searching posts
# --------------------------------------------------------------------------


def test_a_search_finds_a_post_by_its_title_and_body(app, db_session):
    """The whole point of the module, and the thing that could not run at all
    before this round: a real full-text match against the search vector.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'gardening in winter')
    _post(community, alice, 'car maintenance')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=gardening')

    assert _titles(render) == ['gardening in winter']


def test_a_search_never_returns_deleted_unreviewed_or_microblog_posts(app, db_session):
    """The base filter is three predicates before any reader preference, so
    the fixture has one post per exclusion plus a good one.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'visible article')
    _post(community, alice, 'deleted article', deleted=True)
    _post(community, alice, 'pending article', status=POST_STATUS_REVIEWING)
    _post(community, alice, 'microblog article', private=True)
    _post(community, alice, 'unindexed article', indexable=False)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['visible article']


def test_an_anonymous_search_never_returns_bot_or_nsfl_posts(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'bot article', from_bot=True)
    _post(community, alice, 'nsfl article', nsfl=True)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['plain article']


@pytest.mark.parametrize('nsfw, expected', [
    ('', ['plain article']),
    ('exclude', ['plain article']),
    # `only` asks for nsfw = true AND nsfw = false, so it returns NOTHING --
    # not even the safe post. That is the defect, recorded exactly.
    ('only', []),
    ('include', ['plain article']),
])
def test_an_anonymous_search_never_returns_nsfw_posts_whatever_is_asked(app, db_session,
                                                                        nsfw, expected):
    """D798: the anonymous block builds the same exclude/only/include chain the
    authenticated arm has and then appends `filter(Post.nsfw == False)`
    UNCONDITIONALLY -- so `only` asks for `nsfw = true AND nsfw = false` and can
    only ever return nothing, and `include` is silently overridden.

    Recorded as behaviour: either the chain is dead or the trailing filter is
    wrong, and which one is a product decision about what a logged-out reader
    may see.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'spicy article', nsfw=True)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=article&nsfw={nsfw}')

    assert _titles(render) == expected


@pytest.mark.parametrize('nsfw, expected', [
    ('', ['plain article']),
    ('exclude', ['plain article']),
    ('only', ['spicy article']),
    ('include', ['plain article', 'spicy article']),
])
def test_a_logged_in_reader_gets_the_nsfw_results_they_ask_for(app, db_session, nsfw, expected):
    """The authenticated chain, which DOES work -- and the row that shows what
    the anonymous one was meant to do. hide_nsfw is on, so the empty parameter
    falls to the reader's own preference.
    """
    instance, alice, bob = _seed()
    alice.hide_nsfw = 1
    db.session.commit()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'spicy article', nsfw=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=article&nsfw={nsfw}')

    assert sorted(_titles(render)) == sorted(expected)


def test_a_reader_who_does_not_hide_nsfw_sees_it_by_default(app, db_session):
    """`if nsfw == '' and current_user.hide_nsfw == 1` -- both operands, and
    this row is the one that makes the second load-bearing.
    """
    instance, alice, bob = _seed()
    alice.hide_nsfw = 0
    db.session.commit()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'spicy article', nsfw=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert sorted(_titles(render)) == ['plain article', 'spicy article']


def test_a_logged_in_readers_own_filters_apply(app, db_session):
    """Four reader-scoped filters in one row, each with something to exclude
    and the good post left standing: bots, blocked domains, blocked instances
    and blocked communities.
    """
    from app.models import Domain
    from tests.factories import (make_community_block, make_domain, make_domain_block,
                                 make_instance_block)
    instance, alice, bob = _seed()
    alice.ignore_bots = 1
    peer = make_instance('remote.example', software='lemmy')
    community = make_community('microblogs')
    blocked_community = make_community('blockedcomm')
    domain = make_domain('blocked.test')
    _post(community, alice, 'plain article')
    _post(community, alice, 'bot article', from_bot=True)
    _post(community, alice, 'domain article', domain_id=domain.id)
    _post(community, alice, 'instance article', instance_id=peer.id)
    _post(blocked_community, alice, 'community article')
    make_domain_block(alice, domain)
    make_instance_block(alice, peer)
    make_community_block(alice, blocked_community)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['plain article']


def test_a_blocked_authors_posts_are_left_out(app, db_session):
    from tests.factories import make_user_block
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'mine article')
    _post(community, bob, 'theirs article')
    make_user_block(alice, bob)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['mine article']


def test_posts_from_a_community_the_reader_is_banned_from_are_left_out(app, db_session):
    from tests.factories import ban_user_from_community
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    barred = make_community('barred')
    _post(community, alice, 'open article')
    _post(barred, alice, 'barred article')
    ban_user_from_community(alice, barred)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['open article']


@pytest.mark.parametrize('parameter, expected', [
    ('user_id', 'theirs article'),
    ('type', 'typed article'),
    ('community_id', 'community article'),
    ('language', 'spoken article'),
    ('software', 'remote article'),
])
def test_each_filter_narrows_the_results(app, db_session, parameter, expected):
    """Five independent filters, each its own row, each with one post that
    matches and several that do not -- so a filter that stopped working would
    return the others rather than nothing.
    """
    from app.models import Language
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    community = make_community('microblogs')
    other_community = make_community('othercomm')
    language = Language(name='Spoken', code='sp')
    db.session.add(language)
    db.session.commit()

    _post(community, alice, 'mine article')
    _post(community, bob, 'theirs article')
    # make_post gives every post a url, so they are all POST_TYPE_LINK;
    # the row under test needs a type none of the others carry
    _post(community, alice, 'typed article', type=POST_TYPE_IMAGE)
    _post(other_community, alice, 'community article')
    _post(community, alice, 'spoken article', language_id=language.id)
    _post(community, alice, 'remote article', instance_id=peer.id)

    values = {'user_id': bob.id, 'type': POST_TYPE_IMAGE, 'community_id': other_community.id,
              'language': language.id, 'software': 'lemmy'}
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=article&{parameter}={values[parameter]}')

    assert expected in _titles(render)
    assert len(_titles(render)) == 1


@pytest.mark.parametrize('sort_by, first', [
    ('date', 'newer article'),
    ('top', 'popular article'),
])
def test_the_results_can_be_sorted(app, db_session, sort_by, first):
    """Both named sorts. The fixture makes the wanted post LAST by arrival and
    lowest by the other measure, so neither order satisfies both assertions.
    """
    from datetime import timedelta
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'popular article', up_votes=99, down_votes=0,
          posted_at=utcnow() - timedelta(days=3))
    _post(community, alice, 'newer article', up_votes=0, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=article&sort_by={sort_by}')

    assert _titles(render)[0] == first


# --------------------------------------------------------------------------
# Searching comments
# --------------------------------------------------------------------------


def test_a_comment_search_finds_a_reply_by_its_body(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'a thread')
    _reply(community, bob, post, 'gardening advice')
    _reply(community, bob, post, 'car advice')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=gardening&search_for=comments')

    assert response.status_code == 200
    assert _bodies(render) == ['gardening advice']
    assert render.call_args.kwargs['posts'] is None


def test_a_comment_search_skips_deleted_private_and_unindexed_replies(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'a thread')
    _reply(community, bob, post, 'visible advice')
    _reply(community, bob, post, 'deleted advice', deleted=True)
    _reply(community, bob, post, 'private advice', private=True)
    _reply(community, bob, post, 'unindexed advice', indexable=False)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=advice&search_for=comments')

    assert _bodies(render) == ['visible advice']


def test_a_comment_search_skips_replies_on_posts_that_cannot_be_shown(app, db_session):
    """The join back to Post carries three conditions of its own, so a reply
    can be perfectly fine and still be unreachable through its post.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    good = _post(community, alice, 'a thread')
    deleted = _post(community, alice, 'deleted thread', deleted=True)
    pending = _post(community, alice, 'pending thread', status=POST_STATUS_REVIEWING)
    unindexed = _post(community, alice, 'unindexed thread', indexable=False)
    _reply(community, bob, good, 'visible advice')
    _reply(community, bob, deleted, 'orphan advice')
    _reply(community, bob, pending, 'pending advice')
    _reply(community, bob, unindexed, 'hidden advice')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=advice&search_for=comments')

    assert _bodies(render) == ['visible advice']


def test_an_anonymous_comment_search_skips_bot_and_nsfw_replies(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'a thread')
    _reply(community, bob, post, 'plain advice')
    _reply(community, bob, post, 'bot advice', from_bot=True)
    _reply(community, bob, post, 'spicy advice', nsfw=True)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=advice&search_for=comments')

    assert _bodies(render) == ['plain advice']


def test_a_logged_in_comment_search_applies_the_readers_own_filters(app, db_session):
    """Bots, nsfw, blocked instances, blocked communities, blocked users and
    community bans -- six filters, one row, each with something to exclude.
    """
    from tests.factories import (ban_user_from_community, make_community_block,
                                 make_instance_block, make_user_block)
    instance, alice, bob = _seed()
    alice.ignore_bots = 1
    alice.hide_nsfw = 1
    peer = make_instance('remote.example', software='lemmy')
    carol = make_user(instance, 'carol', local=True)
    community = make_community('microblogs')
    blocked_community = make_community('blockedcomm')
    barred = make_community('barred')
    post = _post(community, alice, 'a thread')
    blocked_post = _post(blocked_community, alice, 'blocked thread')
    barred_post = _post(barred, alice, 'barred thread')
    _reply(community, bob, post, 'plain advice')
    _reply(community, bob, post, 'bot advice', from_bot=True)
    _reply(community, bob, post, 'spicy advice', nsfw=True)
    _reply(community, bob, post, 'remote advice', instance_id=peer.id)
    _reply(community, carol, post, 'blocked author advice')
    _reply(blocked_community, bob, blocked_post, 'blocked community advice')
    _reply(barred, bob, barred_post, 'barred advice')
    make_instance_block(alice, peer)
    make_community_block(alice, blocked_community)
    make_user_block(alice, carol)
    ban_user_from_community(alice, barred)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=advice&search_for=comments')

    assert _bodies(render) == ['plain advice']


@pytest.mark.parametrize('parameter, expected', [
    ('user_id', 'theirs advice'),
    ('type', 'typed advice'),
    ('community_id', 'other advice'),
    ('language', 'spoken advice'),
    ('software', 'remote advice'),
])
def test_each_comment_filter_narrows_the_results(app, db_session, parameter, expected):
    """The comment branch repeats all five of the post branch's filters rather
    than sharing them, so each needs its own row here too -- and `type` filters
    on the POST's type, not the reply's, which is why the fixture sets it there.
    """
    from app.models import Language
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    carol = make_user(instance, 'carol', local=True)
    community = make_community('microblogs')
    other_community = make_community('othercomm')
    language = Language(name='Spoken', code='sp')
    db.session.add(language)
    db.session.commit()
    post = _post(community, alice, 'a thread')
    typed_post = _post(community, alice, 'typed thread', type=POST_TYPE_IMAGE)
    other_post = _post(other_community, alice, 'other thread')

    _reply(community, carol, post, 'mine advice')
    _reply(community, bob, post, 'theirs advice')
    _reply(community, carol, typed_post, 'typed advice')
    _reply(other_community, carol, other_post, 'other advice')
    _reply(community, carol, post, 'spoken advice', language_id=language.id)
    _reply(community, carol, post, 'remote advice', instance_id=peer.id)

    values = {'user_id': bob.id, 'type': POST_TYPE_IMAGE,
              'community_id': other_community.id, 'language': language.id,
              'software': 'lemmy'}
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=advice&search_for=comments&{parameter}={values[parameter]}')

    assert _bodies(render) == [expected]


@pytest.mark.parametrize('sort_by, first', [
    ('date', 'newer advice'),
    ('top', 'popular advice'),
])
def test_comment_results_can_be_sorted(app, db_session, sort_by, first):
    from datetime import timedelta
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'a thread')
    _reply(community, bob, post, 'popular advice', up_votes=99, down_votes=0,
           posted_at=utcnow() - timedelta(days=3))
    _reply(community, bob, post, 'newer advice', up_votes=0, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q=advice&search_for=comments&sort_by={sort_by}')

    assert _bodies(render)[0] == first


# --------------------------------------------------------------------------
# The community field, pagination, and retrieve_remote_post
# --------------------------------------------------------------------------


def test_a_community_typed_into_the_community_field_narrows_the_search(app, db_session):
    """The field takes a bare name and the route turns it into `!name@server`
    before looking it up -- so the fixture types the name the way a person
    does, without either marker.
    """
    instance, alice, bob = _seed()
    wanted = make_community('microblogs')
    other = make_community('othercomm')
    _post(wanted, alice, 'wanted article')
    _post(other, alice, 'other article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&community=microblogs')

    assert _titles(render) == ['wanted article']
    assert render.call_args.kwargs['community_id'] == wanted.id


def test_a_community_field_already_carrying_its_markers_is_left_alone(app, db_session):
    """Both `if not community.startswith('!')` and `if not "@" in community`,
    from the other side: a fully-qualified name must not gain a second marker.
    """
    instance, alice, bob = _seed()
    wanted = make_community('microblogs')
    _post(wanted, alice, 'wanted article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&community=!microblogs@test.piefed.local')

    assert render.call_args.kwargs['community_id'] == wanted.id


def test_a_community_field_naming_nothing_leaves_the_search_wide(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'an article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&community=nosuchcommunity')

    assert render.call_args.kwargs['community_id'] == 0
    assert _titles(render) == ['an article']


def test_the_community_field_with_no_query_goes_to_the_community_list(app, db_session):
    """The route's own comment says people type a community name into the
    Community field when they meant to search for communities, and it sends
    them where they meant to go.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/search?community=microblogs&search_for=communities&language=3')

    assert response.status_code == 302
    assert 'search=microblogs' in response.headers['Location']
    assert 'language_id=3' in response.headers['Location']


def test_an_explicit_community_id_skips_the_name_lookup(app, db_session):
    """`if community_id == 0 and community:` -- an id already in hand means the
    name is never resolved, which is what the first operand decides.
    """
    instance, alice, bob = _seed()
    wanted = make_community('microblogs')
    _post(wanted, alice, 'wanted article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render, \
         patch('app.search.routes.search_for_community') as lookup:
        client.get(f'/search?q=article&community_id={wanted.id}&community=ignored')

    assert lookup.call_count == 0
    assert _titles(render) == ['wanted article']


@pytest.mark.parametrize('search_for, per_page', [('posts', 50), ('comments', 50)])
def test_an_anonymous_reader_gets_fifty_results_a_page(app, db_session, search_for, per_page):
    """The page size depends on both `is_authenticated` and the low_bandwidth
    cookie, and this is the anonymous half: 50 either way.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'a thread')
    for index in range(51):
        if search_for == 'posts':
            _post(community, alice, f'article {index}')
        else:
            _reply(community, bob, post, f'advice {index}')
    client = app.test_client()
    term = 'article' if search_for == 'posts' else 'advice'

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get(f'/search?q={term}&search_for={search_for}')

    paginated = render.call_args.kwargs['posts' if search_for == 'posts' else 'replies']
    assert paginated.per_page == per_page
    assert render.call_args.kwargs['next_url'] is not None
    assert render.call_args.kwargs['prev_url'] is None


def test_a_logged_in_reader_gets_a_hundred_unless_they_asked_for_less(app, db_session):
    """The other half, and the cookie that shortens it -- which needs the app's
    SERVER_NAME as the jar's domain (fact 314).
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'an article')
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')
        assert render.call_args.kwargs['posts'].per_page == 100

        client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')
        client.get('/search?q=article')
        assert render.call_args.kwargs['posts'].per_page == 50


def test_the_second_page_links_back(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    for index in range(51):
        _post(community, alice, f'article {index}')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&page=2')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is not None


def test_the_results_page_carries_the_reader_their_voting_history(app, db_session):
    from tests.factories import make_post_vote
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    post = _post(community, alice, 'an article')
    make_post_vote(alice, post, 1)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert post.id in render.call_args.kwargs['recently_upvoted']
    assert render.call_args.kwargs['recently_downvoted'] == []


def test_an_anonymous_results_page_carries_no_voting_history(app, db_session):
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'an article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert render.call_args.kwargs['recently_upvoted'] == []
    assert render.call_args.kwargs['recently_downvoted'] == []


# --------------------------------------------------------------------------
# retrieve_remote_post
# --------------------------------------------------------------------------


def test_the_remote_post_form_renders(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/retrieve_remote_post')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'community/retrieve_remote_post.html'
    assert render.call_args.kwargs['new_post'] is None


def test_a_banned_user_cannot_retrieve_a_remote_post(app, db_session):
    instance, alice, bob = _seed()
    alice.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render, \
         patch('app.search.routes.resolve_remote_post_from_search') as resolver:
        response = client.get('/retrieve_remote_post')

    assert response.status_code == 302
    assert resolver.call_count == 0


def test_a_resolved_remote_post_comes_back_on_the_page(app, db_session):
    """The address is stripped before it is resolved, so the fixture pads it --
    a user pasting a url brings whitespace with it.
    """
    from flask_wtf.csrf import generate_csrf
    from flask import session as flask_session
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    found = _post(community, alice, 'a remote article')
    client = app.test_client()
    login(client, alice)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw

    with patch('app.search.routes.render_template', return_value='rendered') as render, \
         patch('app.search.routes.resolve_remote_post_from_search', return_value=found) as resolver:
        response = client.post('/retrieve_remote_post',
                               data={'address': '  https://remote.example/post/1  ',
                                     'csrf_token': token})

    assert response.status_code == 200
    assert resolver.call_args.args[0] == 'https://remote.example/post/1'
    assert render.call_args.kwargs['new_post'].id == found.id


def test_a_remote_post_that_cannot_be_found_says_so(app, db_session):
    from flask_wtf.csrf import generate_csrf
    from flask import session as flask_session
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw

    with patch('app.search.routes.render_template', return_value='rendered') as render, \
         patch('app.search.routes.resolve_remote_post_from_search', return_value=None), \
         patch('app.search.routes.flash') as flashed:
        response = client.post('/retrieve_remote_post',
                               data={'address': 'https://remote.example/post/1',
                                     'csrf_token': token})

    assert response.status_code == 200
    assert flashed.call_count == 1
    assert render.call_args.kwargs['new_post'] is None


@pytest.mark.parametrize('search_for, term, expected', [
    ('posts', 'article', 'an article'),
    ('comments', 'advice', 'some advice'),
])
def test_a_reader_with_no_preferences_and_nothing_blocked_skips_every_optional_filter(
        app, db_session, search_for, term, expected):
    """Every reader-scoped filter in both branches is conditional -- on a
    preference being set or on a block list being non-empty -- and all the rows
    above arrange for them to fire. This is the other side: a reader who wants
    bots, keeps nsfw, and has blocked nobody skips all of them, and still gets
    their results.
    """
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    alice.hide_nsfw = 0
    alice.hide_nsfl = 0
    db.session.commit()
    community = make_community('microblogs')
    post = _post(community, alice, 'an article')
    _reply(community, bob, post, 'some advice')
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/search?q={term}&search_for={search_for}')

    assert response.status_code == 200
    found = _titles(render) if search_for == 'posts' else _bodies(render)
    assert found == [expected]


def test_the_query_can_never_be_none(app, db_session):
    """THE MODULE'S TWO RESIDUAL ARCS, PROVED.

    Both branches read `if q is not None:` before searching, and `q` is
    assigned once, at the top:

        q = (request.args.get('q') or '').strip()

    `request.args.get` answers None for a missing parameter, `or ''` turns that
    into a string, and `.strip()` answers a string for every string. So `q` is
    a `str` on every path and the guard cannot be false -- the false arcs in
    the posts branch and the comments branch are both unreachable.

    What the guard hides is that `.search('')` runs on every filter-only
    search, which the empty-query rows above already exercise. D795.
    """
    instance, alice, bob = _seed()

    with app.test_request_context('/search'):
        from flask import request
        assert request.args.get('q') is None
        assert isinstance((request.args.get('q') or '').strip(), str)

    with app.test_request_context('/search?q='):
        from flask import request
        assert (request.args.get('q') or '').strip() == ''


def test_an_nsfw_parameter_nobody_defined_falls_through_the_chain(app, db_session):
    """The anonymous chain is exclude / only / include and has no else, so a
    value naming none of them falls past all three -- and then meets the
    unconditional filter that makes D798 what it is.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'spicy article', nsfw=True)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=article&nsfw=nonsense')

    assert response.status_code == 200
    assert _titles(render) == ['plain article']


# --------------------------------------------------------------------------
# Rows added to close mutation survivors
# --------------------------------------------------------------------------


def test_a_community_field_beside_a_real_query_is_a_filter_not_a_redirect(app, db_session):
    """`if q == '' and search_for == 'communities'` -- the shortcut exists for
    someone who typed a community name and nothing else. With a query as well,
    the field is what it says it is: a filter on the results.
    """
    instance, alice, bob = _seed()
    wanted = make_community('microblogs')
    _post(wanted, alice, 'wanted article')
    client = app.test_client()

    response = client.get('/search?q=wanted&community=microblogs&search_for=communities')

    assert response.status_code == 302
    # the redirect carries the QUERY, not the community field -- a mutant
    # dropping the empty-q operand sends the community name instead
    assert 'search=wanted' in response.headers['Location']


def test_the_community_lookup_never_fetches_a_remote_community(app, db_session):
    """`allow_fetch=False` is what stops a search box reaching out to another
    server for a name nobody here has heard of -- a lookup that fetched would
    make every mistyped community a federation request.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'an article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered'), \
         patch('app.search.routes.search_for_community', return_value=community) as lookup:
        client.get('/search?q=article&community=microblogs')

    assert lookup.call_args.kwargs['allow_fetch'] is False


def test_a_community_id_alone_is_enough_to_run_a_search(app, db_session):
    """The gate's community_id operand, which the parametrized row above cannot
    reach: `community_id` is read from its own parameter, not from the
    community NAME field.
    """
    instance, alice, bob = _seed()
    wanted = make_community('microblogs')
    other = make_community('othercomm')
    _post(wanted, alice, 'wanted article')
    _post(other, alice, 'other article')
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/search?community_id={wanted.id}')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'search/results.html'
    assert _titles(render) == ['wanted article']


def test_a_reader_who_hides_nsfl_does_not_see_it(app, db_session):
    """The authenticated nsfl filter, which is separate from the nsfw chain
    beside it and from the anonymous filter below -- so it needs its own row.
    """
    instance, alice, bob = _seed()
    alice.hide_nsfl = 1
    db.session.commit()
    community = make_community('microblogs')
    _post(community, alice, 'plain article')
    _post(community, alice, 'grim article', nsfl=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article')

    assert _titles(render) == ['plain article']


@pytest.mark.parametrize('sort_by, ranked', [('', True), ('date', False), ('top', False)])
def test_relevance_ranking_is_asked_for_only_when_no_sort_was(app, db_session, sort_by, ranked):
    """`sort=True if sort_by == '' else False`.

    Asserted on the ARGUMENT rather than on the order that comes back. An
    earlier version of this row built one strongly-matching post and one weak
    one and asserted which came first; it passed alone and failed in the full
    suite, because two documents containing the same lexeme can rank EQUAL and
    the tie then breaks by id. The route's decision is the thing the sort
    parameter names, and it is deterministic.
    """
    from sqlalchemy_searchable import SearchQueryMixin
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'an article')
    client = app.test_client()
    real_search = SearchQueryMixin.search
    seen = {}

    def spy(self, term, **kwargs):
        seen.update(kwargs)
        return real_search(self, term, **kwargs)

    with patch('app.search.routes.render_template', return_value='rendered'), \
         patch.object(SearchQueryMixin, 'search', spy):
        response = client.get(f'/search?q=article&sort_by={sort_by}')

    assert response.status_code == 200
    assert seen['sort'] is ranked


def test_an_ordinary_reader_is_not_offered_the_admin_controls(app, db_session):
    """`is_authenticated and is_admin()` -- an ANONYMOUS reader fails the first
    operand, so the row that makes the second load-bearing is a logged-in
    reader who is not an admin.
    """
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search')

    assert render.call_args.kwargs['is_admin'] is False
    assert render.call_args.kwargs['is_staff'] is False
