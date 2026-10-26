"""Hot feeds on each domain lift that domain's own posts, then its sister domains' (plan C2, task 7).

A post's `instance_id` says where it lives. On the storefront for instance row N it adds OWN_DOMAIN_BOOST to
`ranking`, a sister domain's row adds SISTER_DOMAIN_BOOST, and a remote row adds nothing. `ranking` is a reddit-hot
score (one unit is about 12.5 hours or a tenfold score), so the boost is a lift and not an override. Only `hot` and
`scaled` use it; every other feed sort, and every list, uses the tier only as its last sort key.
"""
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g
from flask_login import login_user
from sqlakeyset import get_page
from sqlalchemy import desc, event, text

from app import db
from app.api.alpha.utils.community import get_community_list
from app.api.alpha.utils.post import get_post_list, get_post_list2
from app.community.server_view_list import paginate_union
from app.models import Community, Post, PostReply, Site, utcnow
from app.storefront import (BOOST_WINDOW, OWN_DOMAIN_BOOST, SISTER_DOMAIN_BOOST, boosted_order_by, boosted_posts,
                            boosted_top_sql, domain_boost_expr, domain_boost_sql, sister_instance_ids, tiered_post_ids)
from app.utils import get_deduped_post_ids, post_ids_to_models
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_search_routes import _post, _reply
from tests.two_domains import ONION, request_on, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

BASE = {'first': 'https://test.piefed.local', 'onion': f'http://{ONION}'}


def seed(community, author, title, **fields):
    now = utcnow()
    post = make_post(community, author, f'https://{title}.example/{uuid.uuid4().hex}')
    post.title = title
    for name, value in dict(dict(reply_count=1, score=0, up_votes=0, down_votes=0, posted_at=now, last_active=now,
                                 ranking=0.0, ranking_scaled=0.0), **fields).items():
        setattr(post, name, value)
    db.session.commit()
    return post


@pytest.fixture
def world(app, two_domains):  # noqa: F811
    """Posts by a first-domain account, an onion account and a remote one, all in the first domain's community, and a viewer on each
    domain. `put` seeds a post with exactly the ranking fields a test names."""
    first, onion = two_domains.domains
    peer = make_instance('peer.example', software='piefed')
    authors = {'first': two_domains.local_user('firstling', first), 'onion': two_domains.local_user('onionist', onion),
               'remote': make_user(peer, 'visitor')}
    viewers = {'first': two_domains.local_user('watcher', first), 'onion': two_domains.local_user('viewer', onion)}
    community = two_domains.local_community('art', first)
    db.session.get(Site, 1).private_instance = False
    db.session.commit()

    def put(who, **fields):
        return seed(community, authors[who], who, **fields)

    on = dict(zip(('first', 'onion'), two_domains.domains))

    def ids(domain, sort):
        with request_on(app, on[domain]):
            login_user(viewers[domain])
            return get_deduped_post_ids(uuid.uuid4().hex, [community.id], sort)

    def models(domain, sort):
        with request_on(app, on[domain]):
            return [p.title for p in post_ids_to_models(ids_all(), sort)]

    def ids_all():
        return [p.id for p in Post.query.all()]

    return SimpleNamespace(put=put, ids=ids, models=models, first=first, onion=onion, community=community,
                           authors=authors, viewers=viewers, on=on, app=app)


def titles(ids):
    return [db.session.get(Post, i).title for i in ids]


@pytest.mark.parametrize('sort, field', [('hot', 'ranking'), ('', 'ranking'), ('scaled', 'ranking_scaled')])
def test_an_onion_post_just_below_a_first_post_leads_on_the_onion_only(world, sort, field):
    world.put('first', **{field: 5.0})
    world.put('onion', **{field: 4.8})
    assert titles(world.ids('onion', sort)) == ['onion', 'first']    # 5.8 against 5.5
    assert titles(world.ids('first', sort)) == ['first', 'onion']     # 6.0 against 5.3


@pytest.mark.parametrize('sort, field', [('hot', 'ranking'), ('scaled', 'ranking_scaled')])
def test_a_domains_own_post_leads_a_remote_post_a_little_above_it(world, sort, field):
    """Without the boost the remote post (5.3) leads; with it the first domain's (6.0) does, and the onion's (5.5) does not."""
    world.put('first', **{field: 5.0})
    world.put('onion', **{field: 5.0})
    world.put('remote', **{field: 5.3})
    assert titles(world.ids('first', sort)) == ['first', 'onion', 'remote']
    assert titles(world.ids('onion', sort)) == ['onion', 'first', 'remote']


def test_a_remote_post_a_whole_boost_above_still_leads(world):
    """The lift is bounded: a unit of ranking is about twelve and a half hours."""
    world.put('first', ranking=5.0)
    world.put('remote', ranking=6.1)
    assert titles(world.ids('first', 'hot')) == ['remote', 'first']


@pytest.mark.parametrize('sort', ['hot', 'scaled'])
def test_the_page_orders_as_the_id_selection_does(world, sort):
    """post_ids_to_models re-sorts the ids the selection chose; the two must agree on each storefront."""
    for who, ranking in (('first', 5.0), ('onion', 4.8), ('remote', 5.3)):
        world.put(who, ranking=ranking, ranking_scaled=ranking)
    for domain in ('first', 'onion'):
        assert world.models(domain, sort) == titles(world.ids(domain, sort))


@pytest.mark.parametrize('sort', ['new', 'old', 'active', 'top', 'top_1h', 'top_all'])
def test_the_other_sorts_do_not_depend_on_the_storefront(world, sort):
    now = utcnow()
    world.put('first', posted_at=now - timedelta(minutes=1), last_active=now - timedelta(minutes=3), score=1, ranking=9.0)
    world.put('onion', posted_at=now - timedelta(minutes=3), last_active=now - timedelta(minutes=1), score=3, ranking=1.0)
    world.put('remote', posted_at=now - timedelta(minutes=2), last_active=now - timedelta(minutes=2), score=2, ranking=5.0)
    assert world.ids('first', sort) == world.ids('onion', sort)
    assert titles(world.ids('first', sort)) == {
        'new': ['first', 'remote', 'onion'], 'old': ['onion', 'remote', 'first'], 'active': ['onion', 'remote', 'first'],
        'top': ['onion', 'remote', 'first'], 'top_1h': ['onion', 'remote', 'first'],
        'top_all': ['onion', 'remote', 'first']}[sort]


# ------------------------------------------------------------------------ the API sorts

def api_titles(world, domain, function, sort, **data):
    viewer = world.viewers[domain]
    with request_on(world.app, world.on[domain]):
        g.site = db.session.get(Site, 1)
        g.admin_ids = []
        res = function(f'Bearer {viewer.encode_jwt_token()}', dict(sort=sort, limit=50, **data))
        return [entry['post']['title'] for entry in res['posts']]


@pytest.fixture
def api(world, app):
    world.put('first', ranking=5.0, ranking_scaled=5.0)
    world.put('onion', ranking=4.8, ranking_scaled=4.8)
    return world


@pytest.mark.parametrize('sort', ['Hot', 'Scaled'])
@pytest.mark.parametrize('function, extra', [
    (get_post_list, {}),                                      # the raw SQL listing
    (get_post_list, {'community_name': 'art@test.piefed.local'}),              # the ORM one
    (get_post_list2, {}),
])
def test_the_api_lists_order_as_the_feeds_do(api, sort, function, extra):
    extra = dict(extra, **({'ignore_sticky': True} if function is get_post_list2 else {}))
    assert api_titles(api, 'onion', function, sort, **extra) == ['onion', 'first']
    assert api_titles(api, 'first', function, sort, **extra) == ['first', 'onion']


# ------------------------------------------------------------------------ community lists and search

def test_a_community_list_puts_its_own_domains_community_first(app, two_domains):  # noqa: F811
    first, onion = two_domains.domains
    user = two_domains.local_user('lister', first)
    stamp = utcnow()
    for name, domain in (('a_onion', onion), ('b_first', first)):
        two_domains.local_community(name, domain).last_active = stamp
    db.session.get(Site, 1).private_instance = False
    db.session.commit()

    def listing(domain):
        with request_on(app, domain):
            g.site, g.admin_ids = db.session.get(Site, 1), []
            return [c['community']['name'] for c in get_community_list(f'Bearer {user.encode_jwt_token()}',
                                                                      {'type_': 'All'})['communities']]

    assert listing(first) == ['b_first']   # follow-up F6: the onion's community is not listed off the onion
    assert listing(onion) == ['a_onion', 'b_first']


@pytest.mark.parametrize('sort_by, kind', [('top', 'posts'), ('date', 'posts'), ('top', 'comments'), ('date', 'comments')])
def test_a_search_tie_goes_to_the_domains_own_content(world, app, sort_by, kind):
    for who in ('onion', 'first'):
        post = _post(world.community, world.authors[who], f'{who} article', instance_id=world.authors[who].instance_id,
                     up_votes=1, posted_at=utcnow())
        if kind == 'comments':
            _reply(world.community, world.authors[who], post, f'{who} advice', instance_id=post.instance_id,
                   up_votes=1)
    posts = Post.query.all()
    for post in posts:    # a tie on the sort key, so only the tier orders them
        post.posted_at = utcnow().replace(microsecond=0) - timedelta(hours=1)
    for reply in PostReply.query.all():
        reply.posted_at = utcnow().replace(microsecond=0) - timedelta(hours=1)
    db.session.commit()
    query = 'article' if kind == 'posts' else 'advice'

    def shown(domain):
        with patch('app.search.routes.render_template', return_value='rendered') as render:
            app.test_client().get(f'/search?q={query}&sort_by={sort_by}&search_for={kind}', base_url=BASE[domain])
        found = render.call_args.kwargs['posts' if kind == 'posts' else 'replies'].items
        return [(p.title if kind == 'posts' else p.body) for p in found]

    assert [t.split()[0] for t in shown('first')][0] == 'first'
    assert [t.split()[0] for t in shown('onion')][0] == 'onion'


# ------------------------------------------------------------------------ no request, and no sisters

def raw_order(alias_sql):
    return [r[0] for r in db.session.execute(text(alias_sql[0]), alias_sql[1]).all()]


def test_outside_a_request_row_one_is_own_and_the_boost_is_exactly_one_unit(app, db_session):
    """A celery task has no storefront: row 1 is the current domain. A row-1 post of 5.0 beats a remote 5.9 and loses
    to a remote 6.1, in the SQL and in its SQLAlchemy twin alike."""
    home = make_instance('test.piefed.local', software='piefed')
    peer = make_instance('peer.example', software='piefed')
    mine, theirs = make_user(home, 'mine', local=True), make_user(peer, 'theirs')
    community = make_community('boosted')
    seed(community, mine, 'mine', ranking=5.0)
    remote = seed(community, theirs, 'theirs', ranking=5.9)
    boost, params = domain_boost_sql('p')
    sql = (f'SELECT p.title FROM post p ORDER BY p.ranking + {boost} DESC', params)
    assert raw_order(sql) == ['mine', 'theirs']
    twin = [p.title for p in Post.query.order_by((Post.ranking + domain_boost_expr(Post.instance_id)).desc())]
    assert twin == ['mine', 'theirs']
    remote.ranking = 5.0 + OWN_DOMAIN_BOOST + 0.1
    db.session.commit()
    assert raw_order(sql) == ['theirs', 'mine']
    assert OWN_DOMAIN_BOOST > SISTER_DOMAIN_BOOST > 0
    assert sister_instance_ids() == frozenset()
    assert 'sister_instances' not in params and ' IN ' not in boost


# ------------------------------------------------------------------------ the merge of tiers

@pytest.fixture
def crowd(world):
    """More posts than a page, remote, own and sister alike, with rankings close enough that the boosts interleave
    the tiers, and no two posts equal on any sort key."""
    now = utcnow()
    for index in range(18):
        who = ('remote', 'first', 'onion')[index % 3]
        ranking = 5.0 + ((index * 7) % 18) * 0.1
        world.put(who, ranking=ranking, ranking_scaled=ranking + 0.05 * (index % 4), sticky=index % 5 == 0,
                  posted_at=now - timedelta(minutes=index)).title = f'{who}{index}'
    db.session.commit()
    return world


def run(sql, params):
    return [row[0] for row in db.session.execute(text(sql), params).all()]


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('rank, tail, lead', [
    ('ranking', ('posted_at',), ()),
    ('ranking_scaled', ('ranking', 'posted_at'), ()),
    ('ranking', ('posted_at',), ('sticky',)),
])
def test_the_merge_of_tiers_is_the_naive_boosted_order_in_sql(crowd, domain, rank, tail, lead):
    with request_on(crowd.app, crowd.on[domain]):
        from_where = 'FROM "post" AS p WHERE p.deleted = false AND p.status > 0 '
        sql, params = boosted_top_sql('p.id', 't.id', from_where, rank, tail, 7, lead)
        boost, boost_params = domain_boost_sql('p')
        order = ', '.join([*(f'p.{c} DESC' for c in lead), f'p.{rank} + {boost} DESC', *(f'p.{c} DESC' for c in tail)])
        naive = run(f'SELECT p.id {from_where} ORDER BY {order} LIMIT 7', boost_params)
        assert run(sql, params) == naive
        assert len(naive) == 7 and len({titles([i])[0].rstrip('0123456789') for i in naive}) > 1


def test_the_sql_reads_each_tier_by_the_partial_index(crowd):
    with request_on(crowd.app, crowd.on['onion']):
        sql, params = boosted_top_sql('p.id', 't.id', 'FROM "post" AS p WHERE p.deleted = false AND p.status > 0 '
                                      'AND p.instance_sticky is false ', 'ranking', ('posted_at',), 5)
        db.session.execute(text('SET LOCAL enable_seqscan = off; SET LOCAL enable_sort = off'))
        plan = '\n'.join(run(f'EXPLAIN {sql}', params))
    assert plan.count('Index Scan') == 3 and 'Seq Scan' not in plan, plan


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('rank, tail', [('ranking', ('posted_at',)), ('ranking_scaled', ('ranking', 'posted_at'))])
def test_the_merge_of_tiers_pages_as_the_naive_order_does(crowd, domain, rank, tail):
    """Every page of a keyset walk, 4 posts at a time, and an offset read of the first 6, against the naive order."""
    rank, tail = getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(crowd.app, crowd.on[domain]):
        naive = [p.id for p in Post.query.order_by(*boosted_order_by(rank, tail)).all()]
        walked, bookmark = [], None
        while True:
            page = get_page(boosted_posts(Post.query, rank, tail, 4, bookmark), per_page=4, page=bookmark)
            walked += [p.id for p in page]
            if not page.paging.has_next:
                break
            bookmark = page.paging.bookmark_next
        assert walked == naive
        first = boosted_posts(Post.query, rank, tail, 6)
        assert [p.id for p in first.limit(6)] == naive[:6]
        backwards = boosted_posts(Post.query, rank, tail, 4, '<' + bookmark[1:])
        assert [p.id for p in backwards] == naive
        assert [p.id for p in boosted_posts(Post.query, rank, tail, 4, 'garbage')] == naive
        assert [p.id for p in boosted_posts(Post.query, rank, tail, 4, '>')][:5] == naive[:5]    # no place: the first page
        assert [p.id for p in boosted_posts(Post.query, rank, tail, 4, '>1~2')] == naive


# ------------------------------------------------------------------------ the web community list

def test_the_community_list_puts_its_own_domains_community_first(app, two_domains):  # noqa: F811
    first, onion = two_domains.domains
    stamp = utcnow()
    for name, domain in (('a_onion', onion), ('b_first', first)):
        community = two_domains.local_community(name, domain)
        community.last_active, community.title = stamp, 'same'
    db.session.commit()

    def listing(domain):
        with request_on(app, domain):
            page = paginate_union(Community.query, None, 'last_active desc', 1, 50, None)
            return [c.name for c in page.items]

    assert listing(first) == ['b_first', 'a_onion']
    assert listing(onion) == ['a_onion', 'b_first']


# ------------------------------------------------------------------------ ties at a page boundary

@pytest.fixture
def tied(world):
    """Posts equal on every sort key but the id: five remote ones, then (for `mixed`) the first domain's and the onion's whose
    boosted ranking equals the remote ones', so a tie runs across the tiers too."""
    stamp = utcnow().replace(microsecond=0) - timedelta(minutes=5)
    for _ in range(5):
        world.put('remote', ranking=6.0, ranking_scaled=6.0, posted_at=stamp)
    world.put('remote', ranking=4.0, ranking_scaled=4.0, posted_at=stamp)
    return world, stamp


@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('rank, tail', [('ranking', ('posted_at',)), ('ranking_scaled', ('ranking', 'posted_at'))])
def test_a_tie_on_every_key_does_not_cost_a_keyset_page_its_rows(tied, mixed, domain, rank, tail):
    world, stamp = tied
    if mixed:
        for who in ('first', 'onion'):
            for _ in range(3):
                world.put(who, ranking=5.0 if who == 'first' else 5.5, ranking_scaled=5.0 if who == 'first' else 5.5,
                          posted_at=stamp)
    rank, tail = getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(world.app, world.on[domain]):
        naive = [p.id for p in Post.query.order_by(*boosted_order_by(rank, tail)).all()]
        walked, bookmark = [], None
        while True:
            page = get_page(boosted_posts(Post.query, rank, tail, 2, bookmark), per_page=2, page=bookmark)
            walked += [p.id for p in page]
            if not page.paging.has_next:
                break
            bookmark = page.paging.bookmark_next
    assert walked == naive and len(naive) == 6 + 6 * mixed


# ------------------------------------------------------------------------ the route

@pytest.mark.parametrize('domain', ['first', 'onion'])
def test_the_communities_page_lists_its_own_domains_community_first(app, two_domains, domain):  # noqa: F811
    first, onion = two_domains.domains
    viewer = two_domains.local_user('reader', first if domain == 'first' else onion)
    stamp = utcnow()
    for name, home in (('a_onion', onion), ('b_first', first)):
        community = two_domains.local_community(name, home)
        community.last_active, community.title = stamp, 'same'
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    with patch('app.main.routes.render_template', return_value='rendered') as render:
        client = app.test_client()
        with client.session_transaction(base_url=BASE[domain]) as session:
            session['_user_id'], session['_fresh'] = str(viewer.id), True
        response = client.get('/communities?sort_by=last_active%20desc', base_url=BASE[domain])
    assert response.status_code == 200
    names = [c.name for c in render.call_args.kwargs['communities'].items if c.name.endswith(('_first', '_onion'))]
    # follow-up F6: the onion's community is not listed off the onion
    assert names == (['b_first'] if domain == 'first' else ['a_onion', 'b_first'])


# ------------------------------------------------------------------------ a hot-sort cursor that was not handed out

STAMP = 'dt:2026-10-21 22:57:00.381112'


@pytest.mark.parametrize('sort', ['Hot', 'Scaled'])
@pytest.mark.parametrize('cursor', [
    '>x~' + STAMP + '~i:5',                  # no rank
    '>s:high~' + STAMP + '~i:5',             # a rank that is text
    '>true~' + STAMP + '~i:5',               # a rank that is a flag
    '>f:6.0~i:7~i:5',                        # a posted_at that is a number
    '>f:6.0~x~i:5',                          # no posted_at
    '>f:6.0~' + STAMP + '~f:5.5',            # an id that is not whole
    '>f:6.0~' + STAMP + '~x',                # no id
    '>f:6.0~' + STAMP,                       # a value short
    '<f:6.0~' + STAMP + '~s:five',           # backwards, and the id text
])
def test_a_cursor_of_the_wrong_types_is_a_bad_request_not_a_database_error(api, caplog, sort, cursor):
    if sort == 'Scaled':    # its place has a ranking before posted_at
        cursor = cursor.replace('~' + STAMP, '~f:5.0~' + STAMP, 1) if STAMP in cursor else cursor
    with caplog.at_level('ERROR'):
        with pytest.raises(Exception) as refused:
            api_titles(api, 'first', get_post_list2, sort, ignore_sticky=True, page_cursor=cursor)
    assert type(refused.value) is Exception and str(refused.value) == 'invalid page_cursor'
    assert not [r for r in caplog.records if 'sqlalchemy' in r.name or 'psycopg2' in r.name]


# ------------------------------------------------------------------------ a page boundary among the stickies

@pytest.fixture
def pinned(world):
    """Six stickies (community and instance stickies both) ranked below six other posts, remote, own and sister
    alike, so that a page boundary among the stickies has every later non-sticky post ranked above its cursor."""
    now = utcnow()
    for index in range(12):
        who = ('remote', 'first', 'onion')[index % 3]
        ranking = (4.0 if index < 6 else 6.0) + index * 0.1
        world.put(who, ranking=ranking, ranking_scaled=ranking, sticky=index < 6, instance_sticky=index < 6,
                  posted_at=now - timedelta(minutes=index))
    return world


def api_walk(world, domain, sort, **data):
    """Every page of get_post_list2, two posts at a time: the post ids in order, and every cursor it handed out."""
    viewer = world.viewers[domain]
    walked, cursors, cursor = [], [], None
    with request_on(world.app, world.on[domain]):
        while True:
            g.site, g.admin_ids = db.session.get(Site, 1), []
            res = get_post_list2(f'Bearer {viewer.encode_jwt_token()}',
                                 dict(sort=sort, limit=2, **data, **({'page_cursor': cursor} if cursor else {})))
            walked += [entry['post']['id'] for entry in res['posts']]
            if not (cursor := res['next_page']):
                return walked, cursors
            cursors.append(cursor)


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('rank, tail', [('ranking', ('posted_at',)), ('ranking_scaled', ('ranking', 'posted_at'))])
@pytest.mark.parametrize('lead', ['sticky', 'instance_sticky'])
def test_a_keyset_page_boundary_among_the_stickies_keeps_every_row(pinned, domain, rank, tail, lead):
    lead, rank, tail = (getattr(Post, lead),), getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(pinned.app, pinned.on[domain]):
        sticky_first = Post.query.order_by(desc(lead[0]))
        naive = [p.id for p in sticky_first.order_by(*boosted_order_by(rank, tail)).all()]
        walked, bookmark = [], None
        while True:
            page = get_page(boosted_posts(sticky_first, rank, tail, 2, bookmark, lead), per_page=2, page=bookmark)
            walked += [p.id for p in page]
            if not page.paging.has_next:
                break
            bookmark = page.paging.bookmark_next
    assert walked == naive and len(naive) == 12


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('sort, rank, tail', [('Hot', 'ranking', ('posted_at',)),
                                              ('Scaled', 'ranking_scaled', ('ranking', 'posted_at'))])
@pytest.mark.parametrize('lead, data', [('sticky', {'community_name': 'art@test.piefed.local'}),   # a community's
                                        ('instance_sticky', {})])                                 # everything
def test_the_api_walks_a_sticky_led_feed_to_its_end_in_the_boosted_order(pinned, domain, sort, rank, tail, lead,
                                                                         data):
    """Every cursor the API hands out on the walk is one it takes back."""
    lead, rank, tail = getattr(Post, lead), getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(pinned.app, pinned.on[domain]):
        naive = [p.id for p in Post.query.order_by(desc(lead), *boosted_order_by(rank, tail)).all()]
    walked, cursors = api_walk(pinned, domain, sort, **data)
    assert walked == naive and len(naive) == 12 and len(cursors) == 5


# ------------------------------------------------------------------------ two sticky leads

@pytest.fixture
def two_pins(world):
    """Twelve posts, community stickies and instance stickies apart and together, ranked so that the boosts
    interleave the tiers within each of the four lead groups."""
    now = utcnow()
    for index in range(12):
        who = ('remote', 'first', 'onion')[index % 3]
        ranking = 5.0 + ((index * 5) % 12) * 0.1
        world.put(who, ranking=ranking, ranking_scaled=ranking, sticky=index % 4 < 2, instance_sticky=index % 2 == 0,
                  posted_at=now - timedelta(minutes=index))
    return world


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('rank, tail', [('ranking', ('posted_at',)), ('ranking_scaled', ('ranking', 'posted_at'))])
def test_a_keyset_walk_led_by_both_stickies_is_the_naive_order(two_pins, domain, rank, tail):
    lead, rank, tail = (Post.sticky, Post.instance_sticky), getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(two_pins.app, two_pins.on[domain]):
        sticky_first = Post.query.order_by(desc(Post.sticky)).order_by(desc(Post.instance_sticky))
        naive = [p.id for p in sticky_first.order_by(*boosted_order_by(rank, tail)).all()]
        walked, bookmark = [], None
        while True:
            page = get_page(boosted_posts(sticky_first, rank, tail, 2, bookmark, lead), per_page=2, page=bookmark)
            walked += [p.id for p in page]
            if not page.paging.has_next:
                break
            bookmark = page.paging.bookmark_next
    assert walked == naive and len(naive) == 12


@pytest.mark.parametrize('sort, rank, tail', [('Hot', 'ranking', ('posted_at',)),
                                              ('Scaled', 'ranking_scaled', ('ranking', 'posted_at'))])
def test_the_api_walks_a_feed_led_by_both_stickies_in_the_boosted_order(two_pins, sort, rank, tail):
    """A Local listing named by community: its community stickies lead, and its instance stickies after them."""
    rank, tail = getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(two_pins.app, two_pins.on['first']):
        naive = [p.id for p in Post.query.order_by(desc(Post.sticky), desc(Post.instance_sticky),
                                                   *boosted_order_by(rank, tail)).all()]
    walked, cursors = api_walk(two_pins, 'first', sort, type_='Local', community_name='art@test.piefed.local')
    assert walked == naive and len(naive) == 12 and len(cursors) == 5


# ------------------------------------------------------------------------ a cursor number no float can hold

HUGE = 'i:' + '9' * 400


@pytest.mark.parametrize('sort, cursor', [
    ('Hot', f'>{HUGE}~{STAMP}~i:5'),                   # the rank
    ('Hot', f'<{HUGE}~{STAMP}~i:5'),                   # the rank, backwards
    ('Hot', f'>f:nan~{STAMP}~i:5'),
    ('Hot', f'>f:inf~{STAMP}~i:5'),
    ('Scaled', f'>{HUGE}~f:5.0~{STAMP}~i:5'),          # the rank
    ('Scaled', f'>f:6.0~{HUGE}~{STAMP}~i:5'),          # the tail's ranking
    ('Scaled', f'>f:6.0~f:-inf~{STAMP}~i:5'),
    ('Scaled', f'>f:6.0~f:nan~{STAMP}~i:5'),
])
def test_a_cursor_number_no_float_holds_is_a_bad_request(api, caplog, sort, cursor):
    with caplog.at_level('ERROR'):
        with pytest.raises(Exception) as refused:
            api_titles(api, 'first', get_post_list2, sort, ignore_sticky=True, page_cursor=cursor)
    assert type(refused.value) is Exception and str(refused.value) == 'invalid page_cursor'
    assert not [r for r in caplog.records if 'sqlalchemy' in r.name or 'psycopg2' in r.name]


@pytest.mark.parametrize('sort, cursor', [('Hot', f'>{HUGE}~{STAMP}~i:5'), ('Scaled', f'>f:6.0~{HUGE}~{STAMP}~i:5')])
def test_the_route_answers_a_cursor_number_no_float_holds_with_a_400(api, monkeypatch, sort, cursor):
    monkeypatch.setitem(api.app.config, 'ENABLE_ALPHA_API', 'true')
    response = api.app.test_client().get('/api/alpha/post/list2', base_url=BASE['first'],
                                         query_string={'sort': sort, 'ignore_sticky': 'true', 'page_cursor': cursor})
    assert response.status_code == 400
    assert response.get_json()['message'] == 'invalid page_cursor'


# ------------------------------------------------------------------------ the legacy list's page number

def api_pages(world, domain, sort, pages, limit=2, **data):
    """The post ids of get_post_list's numbered pages, in order."""
    viewer = world.viewers[domain]
    walked = []
    with request_on(world.app, world.on[domain]):
        for page in pages:
            g.site, g.admin_ids = db.session.get(Site, 1), []
            res = get_post_list(f'Bearer {viewer.encode_jwt_token()}', dict(sort=sort, limit=limit, page=page, **data))
            walked += [entry['post']['id'] for entry in res['posts']]
    return walked


@pytest.mark.parametrize('domain', ['first', 'onion'])
@pytest.mark.parametrize('sort, rank, tail', [('Hot', 'ranking', ('posted_at',)),
                                              ('Scaled', 'ranking_scaled', ('ranking', 'posted_at'))])
def test_the_legacy_lists_pages_inside_the_window_are_the_naive_boosted_order(crowd, domain, sort, rank, tail):
    rank, tail = getattr(Post, rank), tuple(getattr(Post, c) for c in tail)
    with request_on(crowd.app, crowd.on[domain]):
        naive = [p.id for p in Post.query.order_by(desc(Post.sticky), *boosted_order_by(rank, tail)).all()]
    walked = api_pages(crowd, domain, sort, range(1, 11), community_name='art@test.piefed.local')
    assert walked == naive and len(naive) == 18


@pytest.mark.parametrize('sort', ['Hot', 'Scaled'])
def test_a_legacy_page_past_the_window_reads_no_tier(crowd, sort):
    """A page number a client chose: past BOOST_WINDOW rows the list is the plain order, read straight to its OFFSET,
    and no tier is read that deep."""
    with patch('app.storefront.tiered_post_ids', side_effect=AssertionError('a tier read past the window')):
        assert api_pages(crowd, 'first', sort, [10 ** 6], community_name='art@test.piefed.local') == []
        assert api_pages(crowd, 'first', sort, [BOOST_WINDOW // 2 + 1], community_name='art@test.piefed.local') == []


def test_the_last_page_inside_the_window_is_still_boosted(crowd):
    """page * limit == BOOST_WINDOW is the deepest boosted page: its tiers are read."""
    with patch('app.storefront.tiered_post_ids', wraps=tiered_post_ids) as tiers:
        assert api_pages(crowd, 'first', 'Hot', [BOOST_WINDOW // 2], community_name='art@test.piefed.local') == []
    assert tiers.call_count == 1 and tiers.call_args.args[3] == BOOST_WINDOW + 1


def test_a_tier_reads_post_ids_and_not_whole_posts(crowd):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    with request_on(crowd.app, crowd.on['onion']):
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            tiered_post_ids(Post.query, Post.ranking, (Post.posted_at,), 5)
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
    tiers = [s for s in statements if 'FROM post' in s]
    assert len(tiers) == 3, statements
    assert all(re.match(r'SELECT post\.id(?: AS post_id)? \nFROM post', s) for s in tiers), tiers
