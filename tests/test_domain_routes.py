"""app/domain/routes.py -- the domain page, its feed, and the moderation pairs.

MEASUREMENT BASIS. The module stood at 18.107% on the full-suite --cov=app run
at e37767dc5, carrying 145 missing statements and 54 missing arcs.

Three defects are pinned here and repaired together:

  P1  a dot-less, non-numeric id went to the database as a primary key and was
      a DataError -- a 500 on a public route, in both the page and the feed.
  P2  the feed's de-duplication added the entry before deciding to skip it, so
      a second post sharing a url produced an <item> with no description, guid,
      author or date.
  P3  domain_unblock read the OPTIONAL HX-Current-Url header without a guard.
      This is D756 exactly, repaired in app/chat/routes.py four rounds ago.

Facts 292-294 and 314 apply: render_template is patched for anything that
renders, POST routes need a real CSRF token, the site fixture supplies g.site,
and a cookie needs the app's SERVER_NAME as its domain.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Domain, DomainBlock, Post, Site, utcnow
from tests.factories import (grant_permission, make_community, make_instance, make_post,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _domain(name='example.test', **kwargs):
    domain = Domain(name=name, post_count=0, banned=False)
    for key, value in kwargs.items():
        setattr(domain, key, value)
    db.session.add(domain)
    db.session.commit()
    return domain


def _post_on(domain, community, author, title='an article', url=None, **kwargs):
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    post.domain_id = domain.id
    post.url = url if url is not None else f'https://{domain.name}/{title.replace(" ", "-")}'
    post.status = POST_STATUS_REVIEWING + 1
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.commit()
    domain.post_count = Post.query.filter_by(domain_id=domain.id).count()
    db.session.commit()
    return post


# --------------------------------------------------------------------------
# P1: a dot-less, non-numeric id
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['/d/notanumber', '/d/notanumber/feed'])
def test_an_id_that_is_neither_a_name_nor_a_number_is_a_404(app, db_session, path):
    """Before the repair:

        PROBE d1 exception: DataError (psycopg2.errors.InvalidTextRepresentation)
        invalid input syntax for type integer: "notanumber"

    The route is public and unauthenticated, so any crawler following a mangled
    link reached it. Both copies of the lookup are pinned, because repairing
    one and leaving the other is the shape this campaign has met six times.
    """
    instance, alice, bob = _seed()
    _domain()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered'):
        assert client.get(path).status_code == 404


def test_a_numeric_id_still_finds_its_domain(app, db_session):
    """The other half of P1's inversion: a repair that refused every dot-less
    id would pass the test above.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/d/{domain.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['domain'].id == domain.id


def test_a_name_with_a_dot_still_finds_its_domain(app, db_session):
    """The name branch, which the dot decides. `example.test` never reaches the
    primary-key lookup at all.
    """
    instance, alice, bob = _seed()
    domain = _domain('example.test')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get('/d/example.test')

    assert response.status_code == 200
    assert render.call_args.kwargs['domain'].id == domain.id


# --------------------------------------------------------------------------
# P2: the feed's de-duplication
# --------------------------------------------------------------------------


def test_two_posts_sharing_a_url_give_one_complete_feed_entry(app, db_session):
    """Before the repair the entry was added and THEN skipped:

        PROBE d2 entries: 2 guids: 1 authors: 0

    -- an <item> with a title and a link and nothing else. The assertion counts
    the parts of an entry rather than the entries alone, since a repair that
    emitted two complete items would also give `entries: 2`.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='first', url='https://example.test/same')
    _post_on(domain, community, alice, title='second', url='https://example.test/same')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    # one complete entry: the parts the old `continue` skipped are the
    # assertion, not the item count alone. <author> is not among them -- feedgen
    # omits an RSS author that carries a name and no email -- so pubDate, the
    # last field after the skip, stands in for it.
    assert body.count('<item>') == 1
    assert body.count('<guid') == 1
    assert body.count('<pubDate>') == 1
    assert body.count('<description>') == 1
    # the newer post wins, since the feed is ordered by posted_at descending
    assert '<title>second</title>' in body
    assert '<title>first</title>' not in body


def test_two_posts_with_different_urls_both_appear(app, db_session):
    """The other half: the dedupe must not swallow distinct articles."""
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='first', url='https://example.test/one')
    _post_on(domain, community, alice, title='second', url='https://example.test/two')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert body.count('<item>') == 2
    assert body.count('<guid') == 2
    assert body.count('<pubDate>') == 2


# --------------------------------------------------------------------------
# P3: D756's twin
# --------------------------------------------------------------------------


def test_unblocking_without_a_current_url_still_answers(app, db_session):
    """PROBE d3 exception: TypeError argument of type 'NoneType' is not iterable.

    HX-Current-Url is optional, and by the time it is read the unblock has
    already been written -- so the user's block was removed and the response
    was a 500. D756, in a second module.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    db.session.add(DomainBlock(user_id=alice.id, domain_id=domain.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/d/{domain.id}'
    assert DomainBlock.query.count() == 0


def test_unblocking_from_elsewhere_returns_the_reader_there(app, db_session):
    """The true arm of the same guard, which keeps the `/d/` test
    load-bearing: a current url that is not a domain page comes back unchanged.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/u/bob'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/u/bob'


def test_unblocking_from_a_domain_page_returns_to_that_page(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': f'https://test.piefed.local/d/{domain.id}'})

    assert response.headers['HX-Redirect'] == f'/d/{domain.id}'


# --------------------------------------------------------------------------
# show_domain: which posts a reader sees
# --------------------------------------------------------------------------


def test_a_banned_domain_is_a_404_by_name_and_by_id(app, db_session):
    """Both lookups refuse a banned domain, by different routes: the name query
    filters on it, the id query loads the row and then discards it.
    """
    instance, alice, bob = _seed()
    domain = _domain('banned.test', banned=True)
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered'):
        assert client.get('/d/banned.test').status_code == 404
        assert client.get(f'/d/{domain.id}').status_code == 404


def test_an_unknown_name_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered'):
        assert client.get('/d/nosuch.test').status_code == 404


def test_an_anonymous_reader_never_sees_bot_deleted_or_private_posts(app, db_session):
    """The anonymous post filter is six predicates in one call, so the fixture
    supplies one post per exclusion plus a good one -- with fewer, any subset of
    the filter would pass.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    banned_community = make_community('bannedcomm')
    banned_community.banned = True
    db.session.commit()
    _post_on(domain, community, alice, title='good')
    _post_on(domain, community, alice, title='bot', from_bot=True)
    _post_on(domain, community, alice, title='deleted', deleted=True)
    _post_on(domain, community, alice, title='reviewing', status=POST_STATUS_REVIEWING)
    _post_on(domain, community, alice, title='microblog', private=True)
    _post_on(domain, banned_community, alice, title='banned community')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['good']


def test_a_reader_who_wants_bots_sees_them(app, db_session):
    """The authenticated arm drops the from_bot and private predicates, which
    is what `ignore_bots == 1` decides. A post from a bot is the difference.
    """
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    db.session.commit()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='good')
    _post_on(domain, community, alice, title='bot', from_bot=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert sorted(p.title for p in render.call_args.kwargs['posts'].items) == ['bot', 'good']


def test_a_reader_who_ignores_bots_takes_the_anonymous_filter(app, db_session):
    instance, alice, bob = _seed()
    alice.ignore_bots = 1
    db.session.commit()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='good')
    _post_on(domain, community, alice, title='bot', from_bot=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['good']


def test_a_blocked_instances_posts_are_filtered_out(app, db_session):
    """The instance filter only runs when the reader has blocked something, and
    it deliberately keeps posts with no instance at all -- so the fixture has
    one of each.
    """
    from tests.factories import make_instance_block
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    domain = _domain()
    community = make_community('microblogs')
    blocked = _post_on(domain, community, alice, title='from blocked', instance_id=peer.id)
    _post_on(domain, community, alice, title='no instance', instance_id=None)
    make_instance_block(alice, peer)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['no instance']


def test_a_private_communitys_posts_are_hidden_from_a_stranger(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _post_on(domain, private, alice, title='secret')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert list(render.call_args.kwargs['posts'].items) == []


def test_a_private_communitys_posts_are_shown_to_a_member(app, db_session):
    """Its own test, not a second phase of the one above: a manual login does
    not take after an anonymous request in the same test (fact 315).
    """
    from app.models import CommunityMember
    instance, alice, bob = _seed()
    domain = _domain()
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _post_on(domain, private, alice, title='secret')
    db.session.add(CommunityMember(user_id=alice.id, community_id=private.id,
                                   is_banned=False))
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['secret']


def test_a_reader_hiding_read_posts_does_not_see_them(app, db_session):
    """Both arms of hide_read_posts, and the outer join it adds. The read post
    is marked through the factory so the association row is the real one.
    """
    from tests.factories import mark_post_read
    instance, alice, bob = _seed()
    alice.hide_read_posts = True
    alice.ignore_bots = 0
    db.session.commit()
    domain = _domain()
    community = make_community('microblogs')
    read = _post_on(domain, community, alice, title='already read')
    _post_on(domain, community, alice, title='unread')
    mark_post_read(alice, read)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')
        assert [p.title for p in render.call_args.kwargs['posts'].items] == ['unread']

        alice.hide_read_posts = False
        db.session.commit()
        client.get(f'/d/{domain.id}')
        assert sorted(p.title for p in render.call_args.kwargs['posts'].items) == \
            ['already read', 'unread']


def test_the_page_links_to_its_feed_only_when_there_is_something_in_it(app, db_session):
    """Both rss_feed arguments are conditional on post_count, so a domain with
    no posts offers no feed link at all.
    """
    instance, alice, bob = _seed()
    empty = _domain('empty.test')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{empty.id}')
        assert render.call_args.kwargs['rss_feed'] is None
        assert render.call_args.kwargs['rss_feed_name'] is None

    community = make_community('microblogs')
    _post_on(empty, community, alice, title='something')

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{empty.id}')
        assert render.call_args.kwargs['rss_feed'].endswith(f'/d/{empty.id}/feed')
        assert render.call_args.kwargs['rss_feed_name'].startswith('empty.test on ')


def test_the_pages_of_a_busy_domain_link_to_each_other(app, db_session):
    """The page is 1-based here, unlike the topic page's posts tab -- and
    `prev_url ... and page != 1` is correct for a 1-based paginator, which is
    why this module has no D782.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    for index in range(101):
        _post_on(domain, community, alice, title=f'post {index}')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}?page=1')
        assert render.call_args.kwargs['next_url'] is not None
        assert render.call_args.kwargs['prev_url'] is None
        client.get(f'/d/{domain.id}?page=2')
        assert render.call_args.kwargs['next_url'] is None
        assert render.call_args.kwargs['prev_url'] is not None


def test_a_logged_in_reader_gets_their_voting_history(app, db_session):
    from tests.factories import make_post_vote
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    post = _post_on(domain, community, alice, title='voted on')
    make_post_vote(alice, post, 1)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert post.id in render.call_args.kwargs['recently_upvoted']
    assert render.call_args.kwargs['recently_downvoted'] == []


def test_an_anonymous_reader_gets_no_voting_history_and_no_filters(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get(f'/d/{domain.id}')

    assert render.call_args.kwargs['recently_upvoted'] == []
    assert render.call_args.kwargs['recently_downvoted'] == []
    assert render.call_args.kwargs['content_filters'] == {}
    assert render.call_args.kwargs['form'] is None


# --------------------------------------------------------------------------
# show_domain: the post warning form
# --------------------------------------------------------------------------


def test_an_admin_gets_the_warning_form_filled_from_the_domain(app, db_session):
    instance, alice, bob = _seed()
    _make_admin(alice)
    domain = _domain(post_warning='careful', warning_type=2)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/d/{domain.id}')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.post_warning.data == 'careful'
    assert form.warning_type.data == '2'


def test_an_admin_can_save_a_warning(app, db_session):
    instance, alice, bob = _seed()
    _make_admin(alice)
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.domain.routes.render_template', return_value='rendered'):
        response = client.post(f'/d/{domain.id}',
                               data={'post_warning': 'this site paywalls',
                                     'warning_type': '1', 'csrf_token': token})

    assert response.status_code == 200
    db.session.expire_all()
    saved = Domain.query.get(domain.id)
    assert saved.post_warning == 'this site paywalls'
    assert saved.warning_type == 1


def test_an_ordinary_reader_cannot_save_a_warning(app, db_session):
    """The form is built for staff and admins only, so an ordinary reader's
    POST has nothing to validate and the domain is left alone.
    """
    instance, alice, bob = _seed()
    domain = _domain(post_warning='untouched')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/d/{domain.id}',
                               data={'post_warning': 'sneaky', 'warning_type': '1',
                                     'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is None
    db.session.expire_all()
    assert Domain.query.get(domain.id).post_warning == 'untouched'


def _make_admin(user):
    """Both notions of admin (fact 308): the role's NAME for is_admin() and its
    ID for Site.admins() and g.admin_ids.
    """
    from app.constants import ROLE_ADMIN
    from app.models import Role, user_role
    role = Role.query.get(ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


# --------------------------------------------------------------------------
# show_domain_rss
# --------------------------------------------------------------------------


def test_the_feed_carries_the_domains_posts(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='an article')

    response = app.test_client().get(f'/d/{domain.id}/feed')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/rss+xml'
    assert response.headers['ETag'] == f'{domain.id}_{hash(domain.post_count)}'
    assert response.headers['Cache-Control'] == 'no-cache, max-age=600, must-revalidate'
    assert '<title>an article</title>' in body
    assert f'example.test on ' in body


def test_an_unchanged_feed_answers_304(app, db_session):
    """The ETag is the domain's id and its post count, so a second request
    carrying it gets a 304 with no body -- and a feed whose post count has
    CHANGED does not, which is the half that proves the etag is not a constant.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='an article')
    client = app.test_client()

    first = client.get(f'/d/{domain.id}/feed')
    etag = first.headers['ETag']
    again = client.get(f'/d/{domain.id}/feed', headers={'If-None-Match': etag})
    assert again.status_code == 304

    _post_on(domain, community, alice, title='another article')
    after = client.get(f'/d/{domain.id}/feed', headers={'If-None-Match': etag})
    assert after.status_code == 200


def test_the_feed_never_carries_bot_deleted_or_private_posts(app, db_session):
    """The feed has one filter for everybody -- there is no logged-in variant of
    this route -- so every exclusion is asserted here.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    private_community = make_community('privatecomm')
    private_community.private = True
    banned_community = make_community('bannedcomm')
    banned_community.banned = True
    db.session.commit()
    _post_on(domain, community, alice, title='good')
    _post_on(domain, community, alice, title='bot', from_bot=True)
    _post_on(domain, community, alice, title='deleted', deleted=True)
    _post_on(domain, community, alice, title='reviewing', status=POST_STATUS_REVIEWING)
    _post_on(domain, community, alice, title='microblog', private=True)
    _post_on(domain, private_community, alice, title='private community')
    _post_on(domain, banned_community, alice, title='banned community')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert body.count('<item>') == 1
    assert '<title>good</title>' in body


def test_a_feed_entry_links_to_the_posts_slug_or_its_id(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    slugged = _post_on(domain, community, alice, title='slugged', url='https://example.test/a')
    plain = _post_on(domain, community, alice, title='plain', url='https://example.test/b')
    slugged.slug = '/post/slugged-one'
    plain.slug = None
    db.session.commit()

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert 'https://test.piefed.local/post/slugged-one' in body
    assert f'https://test.piefed.local/post/{plain.id}' in body


def test_a_feed_entry_encloses_media_but_not_a_web_page_or_an_unknown_type(app, db_session):
    """`if type and not type.startswith('text/')` -- three posts separate both
    operands, and the third is a crash test: mimetype_from_url answers None for
    an extension it does not know.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='image', url='https://example.test/p.jpg')
    _post_on(domain, community, alice, title='page', url='https://example.test/a.html')
    _post_on(domain, community, alice, title='unknown', url='https://example.test/x.zzz')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert body.count('<enclosure') == 1
    assert 'p.jpg' in body


def test_a_post_with_no_url_still_gets_an_entry(app, db_session):
    """`if post.url:` false -- a text post on a domain page is unusual but the
    column is nullable, and the entry must still be complete.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='no url', url=None)
    post = Post.query.filter_by(title='no url').one()
    post.url = None
    db.session.commit()

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert body.count('<item>') == 1
    assert body.count('<guid') == 1
    assert '<enclosure' not in body


def test_a_feed_for_a_banned_or_unknown_domain_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    banned = _domain('banned.test', banned=True)
    client = app.test_client()

    assert client.get('/d/banned.test/feed').status_code == 404
    assert client.get(f'/d/{banned.id}/feed').status_code == 404
    assert client.get('/d/nosuch.test/feed').status_code == 404


# --------------------------------------------------------------------------
# the two listings
# --------------------------------------------------------------------------


def test_the_domain_list_shows_unbanned_domains_in_name_order(app, db_session):
    instance, alice, bob = _seed()
    _domain('zebra.test')
    _domain('antelope.test')
    _domain('banned.test', banned=True)
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get('/domains')

    assert response.status_code == 200
    assert [d.name for d in render.call_args.kwargs['domains'].items] == \
        ['antelope.test', 'zebra.test']
    assert render.call_args.kwargs['ban_visibility_permission'] is False


def test_the_domain_list_can_be_searched(app, db_session):
    """The search is a case-insensitive substring, so the query is given in the
    wrong case and matches only one of the two domains.
    """
    instance, alice, bob = _seed()
    _domain('news.example')
    _domain('sport.test')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get('/domains?search=NEWS')

    assert [d.name for d in render.call_args.kwargs['domains'].items] == ['news.example']
    assert render.call_args.kwargs['search'] == 'NEWS'


def test_an_admin_sees_the_ban_controls_on_the_domain_list(app, db_session):
    instance, alice, bob = _seed()
    _make_admin(alice)
    _domain()
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get('/domains')

    assert render.call_args.kwargs['ban_visibility_permission'] is True


def test_the_banned_list_shows_banned_domains_only(app, db_session):
    instance, alice, bob = _seed()
    _domain('ok.test')
    _domain('banned.test', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get('/domains/banned')

    assert response.status_code == 200
    assert [d.name for d in render.call_args.kwargs['domains'].items] == ['banned.test']


def test_the_banned_list_can_be_searched(app, db_session):
    instance, alice, bob = _seed()
    _domain('spam.test', banned=True)
    _domain('malware.test', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        client.get('/domains/banned?search=spam')

    assert [d.name for d in render.call_args.kwargs['domains'].items] == ['spam.test']


@pytest.mark.parametrize('path', ['/domains', '/domains/banned'])
def test_a_second_page_of_domains_links_back(app, db_session, path):
    """Both listings paginate, at 100 and 5000 per page -- so the fixture for
    the banned list would need five thousand rows to reach page 2. Instead the
    page is asked for directly, which is what a link in the wild does, and the
    prev/next pair is read from an empty page 2.
    """
    instance, alice, bob = _seed()
    _domain('one.test', banned=path.endswith('banned'))
    client = app.test_client()
    login(client, alice)

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get(f'{path}?page=2')

    assert response.status_code == 200
    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is not None


# --------------------------------------------------------------------------
# block, unblock, ban, unban
# --------------------------------------------------------------------------


def test_blocking_a_domain_records_it_for_that_reader(app, db_session):
    """The block is per-reader, so the assertion names the user as well as the
    domain -- a block written for the wrong user would pass a count.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/block', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/d/{domain.id}'
    block = DomainBlock.query.one()
    assert block.user_id == alice.id
    assert block.domain_id == domain.id


def test_blocking_over_htmx_answers_with_a_redirect_header(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/block', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/d/{domain.id}'
    assert DomainBlock.query.count() == 1


def test_unblocking_without_htmx_redirects_to_the_domain(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    db.session.add(DomainBlock(user_id=alice.id, domain_id=domain.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/d/{domain.id}'
    assert DomainBlock.query.count() == 0


@pytest.mark.parametrize('path', ['block', 'unblock'])
def test_blocking_an_unknown_domain_is_a_404(app, db_session, path):
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    assert client.post(f'/d/9999/{path}', data={'csrf_token': token}).status_code == 404


def test_banning_a_domain_hides_it_and_purges_its_content(app, db_session):
    """purge_content is the part that matters: a ban that left the posts behind
    would still pass an assertion on the flag alone.
    """
    instance, alice, bob = _seed()
    grant_permission(alice, 'manage users')
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='an article')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/ban', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/domains'
    db.session.expire_all()
    assert Domain.query.get(domain.id).banned is True
    assert Post.query.filter_by(domain_id=domain.id, deleted=False).count() == 0


def test_unbanning_a_domain_makes_it_visible_again(app, db_session):
    instance, alice, bob = _seed()
    grant_permission(alice, 'manage users')
    domain = _domain(banned=True)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unban', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/d/{domain.id}'
    db.session.expire_all()
    assert Domain.query.get(domain.id).banned is False


@pytest.mark.parametrize('path', ['ban', 'unban'])
def test_banning_needs_the_manage_users_permission(app, db_session, path):
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/{path}', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/auth/permission_denied'
    db.session.expire_all()
    assert Domain.query.get(domain.id).banned is False


def test_unban_all_clears_every_ban_at_once(app, db_session):
    """The route is one raw UPDATE over the whole table, so the fixture has two
    banned domains and one that was never banned -- and all three end unbanned,
    which is what the statement says and what an operator gets.
    """
    instance, alice, bob = _seed()
    grant_permission(alice, 'manage users')
    _domain('one.test', banned=True)
    _domain('two.test', banned=True)
    _domain('three.test', banned=False)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post('/domains/unban_all', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/domains/banned'
    db.session.expire_all()
    assert Domain.query.filter_by(banned=True).count() == 0


def test_unban_all_needs_the_manage_users_permission(app, db_session):
    instance, alice, bob = _seed()
    _domain('one.test', banned=True)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post('/domains/unban_all', data={'csrf_token': token})

    assert response.headers['Location'] == '/auth/permission_denied'
    db.session.expire_all()
    assert Domain.query.filter_by(banned=True).count() == 1


def test_the_guards_after_get_or_404_can_never_be_false(app, db_session):
    """THE MODULE'S TWO RESIDUAL ARCS, PROVED.

    `domain_ban` and `domain_unban` both read

        domain = Domain.query.get_or_404(domain_id)
        if domain:
            ...

    and `get_or_404` RAISES for a missing row rather than returning one that is
    falsy -- which the 404 rows above already show from the outside. So the
    guard cannot be false, and the implicit `None` return it hides would be a
    500 rather than a refusal: a Flask view that falls off the end is
    `TypeError: The view function did not return a valid response` (fact 306).

    Demonstrated on the call itself rather than argued, since no request can
    reach the arc: get_or_404 raises NotFound, and a real row is truthy.

    Registered as D784 rather than deleted, following D758 and D773.
    """
    from werkzeug.exceptions import NotFound
    instance, alice, bob = _seed()
    domain = _domain()

    with app.test_request_context('/'):
        with pytest.raises(NotFound):
            Domain.query.get_or_404(9999)
        assert bool(Domain.query.get_or_404(domain.id)) is True
