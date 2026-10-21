"""Hot feeds on each domain lift that domain's own posts, then its sister domains' (plan C2, task 7).

A post's `instance_id` says where it lives. On the storefront for instance row N it adds OWN_DOMAIN_BOOST to
`ranking`, a sister domain's row adds SISTER_DOMAIN_BOOST, and a remote row adds nothing. `ranking` is a reddit-hot
score (one unit is about 12.5 hours or a tenfold score), so the boost is a lift and not an override. Only `hot` and
`scaled` use it; every other feed sort, and every list, uses the tier only as its last sort key.
"""
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g
from flask_login import login_user
from sqlakeyset import get_page
from sqlalchemy import text

from app import db
from app.api.alpha.utils.community import get_community_list
from app.api.alpha.utils.post import get_post_list, get_post_list2
from app.models import Community, Post, Site, utcnow
from app.storefront import (OWN_DOMAIN_BOOST, SISTER_DOMAIN_BOOST, boosted_order_by, boosted_posts, boosted_top_sql,
                            domain_boost_expr, domain_boost_sql, sister_instance_ids)
from app.utils import get_deduped_post_ids, post_ids_to_models
from tests.factories import make_community, make_instance, make_post, make_user
from tests.two_domains import ONION, request_on, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

BASE = {'hell': 'https://test.piefed.local', 'onion': f'http://{ONION}'}


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
    """Posts by a hell account, an onion account and a remote one, all in hell's community, and a viewer on each
    domain. `put` seeds a post with exactly the ranking fields a test names."""
    hell, onion = two_domains.domains
    peer = make_instance('peer.example', software='piefed')
    authors = {'hell': two_domains.local_user('hellion', hell), 'onion': two_domains.local_user('onionist', onion),
               'remote': make_user(peer, 'visitor')}
    viewers = {'hell': two_domains.local_user('watcher', hell), 'onion': two_domains.local_user('viewer', onion)}
    community = two_domains.local_community('art', hell)
    db.session.get(Site, 1).private_instance = False
    db.session.commit()

    def put(who, **fields):
        return seed(community, authors[who], who, **fields)

    on = dict(zip(('hell', 'onion'), two_domains.domains))

    def ids(domain, sort):
        with request_on(app, on[domain]):
            login_user(viewers[domain])
            return get_deduped_post_ids(uuid.uuid4().hex, [community.id], sort)

    def models(domain, sort):
        with request_on(app, on[domain]):
            return [p.title for p in post_ids_to_models(ids_all(), sort)]

    def ids_all():
        return [p.id for p in Post.query.all()]

    return SimpleNamespace(put=put, ids=ids, models=models, hell=hell, onion=onion, community=community,
                           authors=authors, viewers=viewers, on=on, app=app)


def titles(ids):
    return [db.session.get(Post, i).title for i in ids]


@pytest.mark.parametrize('sort, field', [('hot', 'ranking'), ('', 'ranking'), ('scaled', 'ranking_scaled')])
def test_an_onion_post_just_below_a_hell_post_leads_on_the_onion_only(world, sort, field):
    world.put('hell', **{field: 5.0})
    world.put('onion', **{field: 4.8})
    assert titles(world.ids('onion', sort)) == ['onion', 'hell']    # 5.8 against 5.5
    assert titles(world.ids('hell', sort)) == ['hell', 'onion']     # 6.0 against 5.3


@pytest.mark.parametrize('sort, field', [('hot', 'ranking'), ('scaled', 'ranking_scaled')])
def test_a_domains_own_post_leads_a_remote_post_a_little_above_it(world, sort, field):
    """Without the boost the remote post (5.3) leads; with it hell's (6.0) does, and the onion's (5.5) does not."""
    world.put('hell', **{field: 5.0})
    world.put('onion', **{field: 5.0})
    world.put('remote', **{field: 5.3})
    assert titles(world.ids('hell', sort)) == ['hell', 'onion', 'remote']
    assert titles(world.ids('onion', sort)) == ['onion', 'hell', 'remote']


def test_a_remote_post_a_whole_boost_above_still_leads(world):
    """The lift is bounded: a unit of ranking is about twelve and a half hours."""
    world.put('hell', ranking=5.0)
    world.put('remote', ranking=6.1)
    assert titles(world.ids('hell', 'hot')) == ['remote', 'hell']


@pytest.mark.parametrize('sort', ['hot', 'scaled'])
def test_the_page_orders_as_the_id_selection_does(world, sort):
    """post_ids_to_models re-sorts the ids the selection chose; the two must agree on each storefront."""
    for who, ranking in (('hell', 5.0), ('onion', 4.8), ('remote', 5.3)):
        world.put(who, ranking=ranking, ranking_scaled=ranking)
    for domain in ('hell', 'onion'):
        assert world.models(domain, sort) == titles(world.ids(domain, sort))


@pytest.mark.parametrize('sort', ['new', 'old', 'active', 'top', 'top_1h', 'top_all'])
def test_the_other_sorts_do_not_depend_on_the_storefront(world, sort):
    now = utcnow()
    world.put('hell', posted_at=now - timedelta(minutes=1), last_active=now - timedelta(minutes=3), score=1, ranking=9.0)
    world.put('onion', posted_at=now - timedelta(minutes=3), last_active=now - timedelta(minutes=1), score=3, ranking=1.0)
    world.put('remote', posted_at=now - timedelta(minutes=2), last_active=now - timedelta(minutes=2), score=2, ranking=5.0)
    assert world.ids('hell', sort) == world.ids('onion', sort)
    assert titles(world.ids('hell', sort)) == {
        'new': ['hell', 'remote', 'onion'], 'old': ['onion', 'remote', 'hell'], 'active': ['onion', 'remote', 'hell'],
        'top': ['onion', 'remote', 'hell'], 'top_1h': ['onion', 'remote', 'hell'],
        'top_all': ['onion', 'remote', 'hell']}[sort]


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
    world.put('hell', ranking=5.0, ranking_scaled=5.0)
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
    assert api_titles(api, 'onion', function, sort, **extra) == ['onion', 'hell']
    assert api_titles(api, 'hell', function, sort, **extra) == ['hell', 'onion']


# ------------------------------------------------------------------------ community lists and search

def test_a_community_list_puts_its_own_domains_community_first(app, two_domains):  # noqa: F811
    hell, onion = two_domains.domains
    user = two_domains.local_user('lister', hell)
    stamp = utcnow()
    for name, domain in (('a_onion', onion), ('b_hell', hell)):
        two_domains.local_community(name, domain).last_active = stamp
    db.session.get(Site, 1).private_instance = False
    db.session.commit()

    def listing(domain):
        with request_on(app, domain):
            g.site, g.admin_ids = db.session.get(Site, 1), []
            return [c['community']['name'] for c in get_community_list(f'Bearer {user.encode_jwt_token()}',
                                                                      {'type_': 'All'})['communities']]

    assert listing(hell) == ['b_hell', 'a_onion']
    assert listing(onion) == ['a_onion', 'b_hell']


@pytest.mark.parametrize('sort_by, kind', [('top', 'posts'), ('date', 'posts'), ('top', 'comments'), ('date', 'comments')])
def test_a_search_tie_goes_to_the_domains_own_content(world, app, sort_by, kind):
    from tests.test_search_routes import _post, _reply
    for who in ('onion', 'hell'):
        post = _post(world.community, world.authors[who], f'{who} article', instance_id=world.authors[who].instance_id,
                     up_votes=1, posted_at=utcnow())
        if kind == 'comments':
            _reply(world.community, world.authors[who], post, f'{who} advice', instance_id=post.instance_id,
                   up_votes=1)
    posts = Post.query.all()
    for post in posts:    # a tie on the sort key, so only the tier orders them
        post.posted_at = utcnow().replace(microsecond=0) - timedelta(hours=1)
    from app.models import PostReply
    for reply in PostReply.query.all():
        reply.posted_at = utcnow().replace(microsecond=0) - timedelta(hours=1)
    db.session.commit()
    query = 'article' if kind == 'posts' else 'advice'

    def shown(domain):
        with patch('app.search.routes.render_template', return_value='rendered') as render:
            app.test_client().get(f'/search?q={query}&sort_by={sort_by}&search_for={kind}', base_url=BASE[domain])
        found = render.call_args.kwargs['posts' if kind == 'posts' else 'replies'].items
        return [(p.title if kind == 'posts' else p.body) for p in found]

    assert [t.split()[0] for t in shown('hell')][0] == 'hell'
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
        who = ('remote', 'hell', 'onion')[index % 3]
        ranking = 5.0 + ((index * 7) % 18) * 0.1
        world.put(who, ranking=ranking, ranking_scaled=ranking + 0.05 * (index % 4), sticky=index % 5 == 0,
                  posted_at=now - timedelta(minutes=index)).title = f'{who}{index}'
    db.session.commit()
    return world


def run(sql, params):
    return [row[0] for row in db.session.execute(text(sql), params).all()]


@pytest.mark.parametrize('domain', ['hell', 'onion'])
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


@pytest.mark.parametrize('domain', ['hell', 'onion'])
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
        assert [p.id for p in boosted_posts(Post.query, rank, tail, 4, '>1~2')] == naive


# ------------------------------------------------------------------------ the web community list

def test_the_community_list_puts_its_own_domains_community_first(app, two_domains):  # noqa: F811
    from app.community.server_view_list import paginate_union
    hell, onion = two_domains.domains
    stamp = utcnow()
    for name, domain in (('a_onion', onion), ('b_hell', hell)):
        community = two_domains.local_community(name, domain)
        community.last_active, community.title = stamp, 'same'
    db.session.commit()

    def listing(domain):
        with request_on(app, domain):
            page = paginate_union(Community.query, None, 'last_active desc', 1, 50, None)
            return [c.name for c in page.items]

    assert listing(hell) == ['b_hell', 'a_onion']
    assert listing(onion) == ['a_onion', 'b_hell']
