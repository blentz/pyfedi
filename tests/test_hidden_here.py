"""`hidden_here` and its SQL forms (follow-up F6). Owner rulings 2026-10-26: "yes, hide onion from logged-in clearnet
viewers too", admins with "No exception". A local-only domain's accounts, communities and content are shown only on
its own storefront, and there only to a signed-in reader; a logged-out reader never sees them (plan C2).

The row test, the SQLAlchemy clause and the raw SQL must agree on every row, on every storefront, for both kinds of
reader: a page gate and a list over the same row never disagree."""
from types import SimpleNamespace

import pytest
from sqlalchemy import not_, text

from app import db
from app.local_only import content_local_only, hidden_here, hidden_here_clause, hidden_here_sql, \
    without_hidden_here, without_local_only, without_local_only_actors
from app.models import Community, Feed, Post, PostReply, SiteDomain, User
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import make_feed, make_instance, make_local_feed, make_post, make_post_reply, make_user
from tests.test_per_domain_ap_routes import SHOP, onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, ONION, request_on, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

XYZ = 'xyz.onion'   # a second local-only domain
MODELS = (User, Community, Post, PostReply, Feed)
TABLES = {User: '"user"', Community: 'community', Post: 'post', PostReply: 'post_reply', Feed: 'feed'}


def write(author, community, title):
    """A post and a reply to it by `author` in `community`, stamped as creation stamps them."""
    post = make_post(community, author, f'https://{FIRST}/post/{title}', title=title)
    post.local_only = content_local_only(author, community)
    reply = make_post_reply(post, author)
    reply.local_only = content_local_only(author, community, None, post)
    db.session.commit()
    return post, reply


def reply_to(post, parent, author):
    """`author`'s reply to `parent` (a reply, or None for the post), with the path and stamp PostReply.new gives it."""
    reply = make_post_reply(post, author)
    reply.parent_id = parent.id if parent is not None else None
    reply.path = [*(parent.path if parent is not None else [0]), reply.id]
    reply.local_only = content_local_only(author, post.community, parent, post)
    db.session.commit()
    return reply


def reply_chain(post, *authors):
    """A chain of replies under `post`, each answering the one before, by `authors` in turn."""
    replies, parent = [], None
    for author in authors:
        parent = reply_to(post, parent, author)
        replies.append(parent)
    return replies


@pytest.fixture
def world(app, twins, two_domains):  # noqa: F811
    first_bob, first_art, onion_bob, onion_art, shop_bob, shop_art = twins
    peer = make_instance('peer.example')
    remote = make_user(peer, 'remote')
    rows = SimpleNamespace(first_bob=first_bob, first_art=first_art, onion_bob=onion_bob, onion_art=onion_art,
                           shop_bob=shop_bob, remote=remote, legacy=User.query.filter_by(user_name='community-owner').one())
    rows.onion_post, rows.onion_reply = write(onion_bob, onion_art, 'onion-in-onion')
    rows.onion_in_first, rows.onion_in_first_reply = write(onion_bob, first_art, 'onion-in-first')
    rows.first_in_onion, _ = write(first_bob, onion_art, 'first-in-onion')
    rows.first_post, rows.first_reply = write(first_bob, first_art, 'first-in-first')
    rows.remote_post, _ = write(remote, first_art, 'remote-in-first')
    rows.first_under_onion = make_post_reply(rows.onion_in_first, first_bob)   # the first's reply to an onion post
    rows.first_under_onion.local_only = content_local_only(first_bob, first_art, None, rows.onion_in_first)
    # review I6: the onion's reply under the first's post, and the first's reply to that one, deeper down
    rows.onion_under_first, rows.first_under_onion_reply = reply_chain(rows.first_post, onion_bob, first_bob)
    # N-m3: a local-only reply whose path names an ancestor nobody holds any more
    rows.orphaned = reply_to(rows.first_post, None, first_bob)
    rows.orphaned.local_only, rows.orphaned.path = True, [0, 2 ** 30, rows.orphaned.id]
    # and one built under a parent that had no path: `[0, id]` though it answers a reply
    rows.parentless = reply_to(rows.first_post, rows.first_under_onion_reply, first_bob)
    rows.parentless.path = [0, rows.parentless.id]
    db.session.commit()
    rows.two_domains = two_domains
    add_feeds(rows, peer)
    return rows


def add_feeds(rows, peer):
    """Feeds on each domain, by each domain's account, a remote one, and legacy rows filed under no domain: one an onion
    account owns (ruling N-m8), one with no owner, and a first-domain feed an onion account owns."""
    first, onion, shop = (domain_for_host(host) for host in (FIRST, ONION, SHOP))
    rows.onion_feed = make_local_feed('onionfeed', True, onion, rows.onion_bob)
    rows.first_feed = make_local_feed('firstfeed', True, first, rows.first_bob)
    rows.shop_feed = make_local_feed('shopfeed', True, shop, rows.shop_bob)
    rows.onion_owned_first_feed = make_local_feed('onionownedfirst', True, first, rows.onion_bob)
    rows.remote_feed = make_feed(peer, 'remotefeed', public=True)
    rows.legacy_onion_feed = make_local_feed('legacyonion', True, None, rows.onion_bob)
    rows.legacy_feed = make_local_feed('legacynobody', True, None, None)
    rows.legacy_onion_feed.site_domain_id = rows.legacy_feed.site_domain_id = None
    db.session.commit()


@pytest.fixture
def two_onions(world):
    """A second local-only domain, with an account and a community, writing in its own community and the onion's."""
    db.session.add(SiteDomain(domain=XYZ, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    xyz = domain_for_host(XYZ)
    world.xyz_bob = world.two_domains.local_user('bob', xyz)
    world.xyz_art = world.two_domains.local_community('art', xyz)
    world.xyz_post, _ = write(world.xyz_bob, world.xyz_art, 'xyz-in-xyz')
    world.xyz_in_onion, _ = write(world.xyz_bob, world.onion_art, 'xyz-in-onion')
    world.onion_in_xyz, _ = write(world.onion_bob, world.xyz_art, 'onion-in-xyz')
    return world


@pytest.fixture
def closed_default(world):
    """The default domain itself does not federate: it owns the rows no domain was stamped on (`legacy`). Set past the
    lock that keeps a domain with accounts from changing it, as a site configured so from the start would have it."""
    db.session.execute(text('UPDATE site_domain SET federates = false WHERE is_default'))
    db.session.commit()
    clear_domain_cache()
    return world


def ids_by_row(model, signed_in):
    return {row.id for row in db.session.query(model).all() if hidden_here(row, signed_in)}


def ids_by_clause(model, signed_in):
    return set(db.session.scalars(db.select(model.id).where(hidden_here_clause(model, signed_in))))


def ids_kept(model, signed_in):
    everyone = set(db.session.scalars(db.select(model.id)))
    kept = {row.id for row in without_hidden_here(db.session.query(model), model, signed_in)}
    return everyone - kept


def ids_by_sql(model, signed_in):
    table = TABLES[model]
    return set(db.session.scalars(text(f'SELECT t.id FROM {table} t WHERE {hidden_here_sql("t", model, signed_in)}')))


def assert_parity(app, hosts):
    for host in hosts:
        with request_on(app, domain_for_host(host)):
            for signed_in in (False, True):
                for model in MODELS:
                    expected = ids_by_row(model, signed_in)
                    assert ids_by_clause(model, signed_in) == expected, (host, signed_in, model)
                    assert ids_kept(model, signed_in) == expected, (host, signed_in, model)
                    assert ids_by_sql(model, signed_in) == expected, (host, signed_in, model)


def test_the_row_test_the_clause_and_the_sql_agree(app, world):
    assert_parity(app, (FIRST, ONION, SHOP))


def test_they_agree_with_two_local_only_domains(app, two_onions):
    assert_parity(app, (FIRST, ONION, SHOP, XYZ))


def test_they_agree_when_the_default_domain_is_local_only(app, closed_default):
    assert_parity(app, (FIRST, ONION, SHOP))


def hidden(app, host, signed_in, rows):
    with request_on(app, domain_for_host(host)):
        return {name for name, row in rows.items() if hidden_here(row, signed_in)}


def named(world, *names):
    return {name: getattr(world, name) for name in names}


ALL = ('first_bob', 'first_art', 'onion_bob', 'onion_art', 'shop_bob', 'remote', 'legacy', 'onion_post',
       'onion_reply', 'onion_in_first', 'onion_in_first_reply', 'first_in_onion', 'first_post', 'first_reply',
       'remote_post', 'first_under_onion', 'onion_under_first', 'first_under_onion_reply', 'orphaned', 'parentless',
       'onion_feed', 'first_feed', 'shop_feed', 'onion_owned_first_feed', 'remote_feed', 'legacy_onion_feed',
       'legacy_feed')
# a reply to an onion post goes with its post (follow-up F5's rule), and one further down an onion reply goes with
# that reply (review I6): it could only have been written on the onion
ONIONS = {'onion_bob', 'onion_art', 'onion_post', 'onion_reply', 'onion_in_first', 'onion_in_first_reply',
          'first_in_onion', 'first_under_onion', 'onion_under_first', 'first_under_onion_reply', 'orphaned', 'parentless',
          'onion_feed', 'onion_owned_first_feed', 'legacy_onion_feed'}   # a feed goes with its domain and its owner (N-m8)


@pytest.mark.parametrize('host', [FIRST, SHOP])
@pytest.mark.parametrize('signed_in', [False, True])
def test_off_the_onion_its_rows_are_hidden_from_every_reader(app, world, host, signed_in):
    assert hidden(app, host, signed_in, named(world, *ALL)) == ONIONS


def test_on_the_onion_a_signed_in_reader_sees_them_all(app, world):
    assert hidden(app, ONION, True, named(world, *ALL)) == set()


def test_on_the_onion_a_logged_out_reader_sees_none_of_them(app, world):
    assert hidden(app, ONION, False, named(world, *ALL)) == ONIONS


def test_on_one_onion_the_other_onions_rows_are_hidden(app, two_onions):
    rows = named(two_onions, 'onion_bob', 'onion_art', 'onion_post', 'xyz_bob', 'xyz_art', 'xyz_post',
                 'xyz_in_onion', 'onion_in_xyz', 'first_in_onion')
    assert hidden(app, ONION, True, rows) == {'xyz_bob', 'xyz_art', 'xyz_post', 'xyz_in_onion', 'onion_in_xyz'}
    assert hidden(app, XYZ, True, rows) == {'onion_bob', 'onion_art', 'onion_post', 'xyz_in_onion', 'onion_in_xyz',
                                            'first_in_onion'}


def test_a_closed_default_domains_legacy_rows_are_hidden_off_it_only(app, closed_default):
    rows = named(closed_default, 'legacy', 'first_bob', 'onion_bob', 'legacy_feed', 'first_feed')
    assert hidden(app, FIRST, True, rows) == {'onion_bob'}
    assert hidden(app, SHOP, True, rows) == {'legacy', 'first_bob', 'onion_bob', 'legacy_feed', 'first_feed'}
    assert hidden(app, ONION, True, rows) == {'legacy', 'first_bob', 'legacy_feed', 'first_feed'}


def test_a_stamped_row_that_belongs_to_no_onion_is_hidden_from_a_logged_out_reader_only(app, world):
    """Plan B's rule stands for a local-only row no local-only domain owns: a signed-in reader sees it anywhere."""
    world.first_post.local_only = True
    db.session.commit()
    for host in (FIRST, ONION):
        assert hidden(app, host, True, named(world, 'first_post')) == set()
        assert hidden(app, host, False, named(world, 'first_post')) == {'first_post'}


def test_with_no_local_only_domain_nothing_is_hidden_and_no_condition_is_added(app, world):
    db.session.execute(text('UPDATE site_domain SET federates = true WHERE domain = :d'), {'d': ONION})   # past the lock
    db.session.commit()
    clear_domain_cache()
    for host in (FIRST, ONION):
        with request_on(app, domain_for_host(host)):
            for model in (User, Community):
                query = db.session.query(model)
                assert without_hidden_here(query, model, False) is query
                assert hidden_here_sql('t', model, False) == 'false'
    with request_on(app, domain_for_host(FIRST)):   # a stamped row stays the logged-out reader's to miss (plan C2)
        assert hidden_here_sql('t', Post, False) == 't.local_only'


def test_the_reader_defaults_to_the_sessions_login(app, world):
    with request_on(app, domain_for_host(ONION)):   # no one is logged in
        assert hidden_here(world.onion_bob) is True
        assert hidden_here(world.onion_post) is True
        assert str(hidden_here_clause(User)) == str(hidden_here_clause(User, False))
        assert hidden_here_sql('t', Post) == 't.local_only'


def test_the_c2_list_filters_go_through_it(app, world):
    with request_on(app, domain_for_host(FIRST)):
        users = {row.id for row in without_local_only_actors(db.session.query(User), User, True)}
        posts = {row.id for row in without_local_only(db.session.query(Post), Post, True)}
    assert world.onion_bob.id not in users and world.first_bob.id in users
    assert world.onion_in_first.id not in posts and world.first_post.id in posts


def test_the_clause_is_never_null_so_its_negation_keeps_every_other_row(app, two_onions):
    with request_on(app, domain_for_host(ONION)):
        for model in MODELS:
            nulls = db.session.scalar(db.select(db.func.count()).select_from(model).where(
                hidden_here_clause(model, True).is_(None)))
            kept = db.session.scalar(db.select(db.func.count()).select_from(model).where(
                not_(hidden_here_clause(model, True))))
            hid = db.session.scalar(db.select(db.func.count()).select_from(model).where(hidden_here_clause(model, True)))
            assert nulls == 0 and kept + hid == db.session.query(model).count(), model


def test_a_reply_further_down_an_onion_reply_is_hidden_with_it(app, world):
    """Review I6: the first's reply, three levels down, to the onion's reply under the first's own post."""
    deeper = reply_to(world.first_post, world.first_under_onion_reply, world.first_bob)
    above = reply_to(world.first_post, None, world.first_bob)   # the control: a reply above the onion's is the first's
    rows = {'deeper': deeper, 'above': above, 'first_post': world.first_post}
    assert hidden(app, FIRST, True, rows) == {'deeper'}
    assert hidden(app, ONION, True, rows) == set()
    assert_parity(app, (FIRST, ONION))


@pytest.mark.parametrize('path', [None, [], [0], 'self only', 'no self'])
def test_a_local_only_reply_whose_path_cannot_be_read_is_hidden_off_the_onion(app, world, path):
    """Fail closed (ruling, N-m3): a clearnet reply is only ever stamped local-only under an onion ancestor, so one
    whose ancestry is unreadable is hidden off the onion. A reply that is not local-only is not touched."""
    reply = world.first_under_onion_reply
    if isinstance(path, str):   # it answers a reply
        path = [0, reply.id] if path == 'self only' else [0, reply.parent_id]
    reply.path = path
    world.first_reply.path = path   # not local-only: the control
    db.session.commit()
    rows = named(world, 'first_under_onion_reply', 'first_reply')
    assert hidden(app, FIRST, True, rows) == {'first_under_onion_reply'}
    assert hidden(app, ONION, True, rows) == set()
    assert_parity(app, (FIRST, ONION, SHOP))


def test_a_local_only_reply_whose_ancestor_was_purged_is_hidden_off_the_onion(app, world):
    """The onion reply above it is gone, so nothing can say whose thread it was: fail closed (N-m3)."""
    deeper = reply_to(world.first_post, world.first_under_onion_reply, world.first_bob)
    gone = world.onion_under_first.id
    world.first_under_onion_reply.path = [0, 2 ** 30, world.first_under_onion_reply.id]   # an ancestor nobody holds
    db.session.commit()
    assert gone   # the chain's own onion reply still stands; the purged id is the one in the path
    rows = {'reply': world.first_under_onion_reply, 'deeper': deeper}
    assert hidden(app, FIRST, True, rows) == {'reply', 'deeper'}
    assert hidden(app, ONION, True, rows) == set()
    assert_parity(app, (FIRST, ONION, SHOP))
