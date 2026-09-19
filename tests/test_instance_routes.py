"""app/instance/routes.py -- the instance list, its people and posts pages, the
bulk follow importer, and the block pair.

MEASUREMENT BASIS. The module stood at 16.573% on the full-suite --cov=app run
at 5f4f8294b, carrying 189 missing statements and 108 missing arcs.

One defect is pinned here:

  P1  instance_unblock assigned the OPTIONAL HX-Current-Url header straight
      into HX-Redirect, so with the header absent werkzeug stringified None and
      htmx navigated to a page called /None -- after the unblock had been
      written. D756's family in a third module, and the only one of the three
      that fails silently.

Facts 292-294 and 314 apply: render_template is patched for anything that
renders, POST routes need a real CSRF token, the site fixture supplies g.site,
and a cookie needs the app's SERVER_NAME as its domain.
"""
import io

import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (AllowedInstances, BannedInstances, Instance, InstanceBlock, Post,
                        Site, User, utcnow)
from tests.factories import make_community, make_instance, make_post, make_user

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
    """The local instance at id 1, the burnt id-1 user, and two locals.

    Instance 1 is the local one everywhere in this codebase, which several of
    these routes rely on -- `/instance/local/people` reads `Instance.query.get(1)`
    and the `federated` filter excludes it by id.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _submitter(user):
    """instance_add_people sits behind validation_required and
    approval_required, so its user needs verified and a private_key.
    """
    user.verified = True
    user.private_key = 'a-private-key'
    db.session.commit()
    return user


def _make_admin(user):
    """Both notions of admin (fact 308)."""
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
# P1: the unblock's redirect header
# --------------------------------------------------------------------------


def test_unblocking_over_htmx_without_a_current_url_names_a_real_page(app, db_session):
    """Before the repair:

        PROBE f3 status: 200 HX-Redirect: 'None'

    -- werkzeug stringifies the None the absent header gives, and htmx then
    navigates to a relative url called `None`. The block was already undone by
    then, so the reader lands on a 404 having succeeded.
    """
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    db.session.add(InstanceBlock(user_id=alice.id, instance_id=peer.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/instance/{peer.domain}'
    assert InstanceBlock.query.count() == 0


def test_unblocking_over_htmx_returns_the_reader_where_they_were(app, db_session):
    """The true arm of the same guard: a current url that IS supplied comes
    back unchanged, which is what keeps the fallback from swallowing it.
    """
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/u/bob'})

    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/u/bob'


# --------------------------------------------------------------------------
# list_instances: the filter algebra
# --------------------------------------------------------------------------


def _instance(domain, **kwargs):
    instance = make_instance(domain, software=kwargs.pop('software', 'lemmy'))
    for key, value in kwargs.items():
        setattr(instance, key, value)
    db.session.commit()
    return instance


def _domains(render):
    return [row.domain for row in render.call_args.kwargs['instances'].items]


def test_the_instance_list_shows_every_instance_in_domain_order(app, db_session):
    instance, alice, bob = _seed()
    _instance('zebra.example')
    _instance('antelope.example')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instances')

    assert response.status_code == 200
    assert _domains(render) == ['antelope.example', 'test.piefed.local', 'zebra.example']
    assert render.call_args.kwargs['allowed_or_blocked'] is False


def test_the_instance_list_can_be_searched(app, db_session):
    """ilike, so the query is given in the wrong case."""
    instance, alice, bob = _seed()
    _instance('news.example')
    _instance('sport.example')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances?search=NEWS')

    assert _domains(render) == ['news.example']
    assert render.call_args.kwargs['search'] == 'NEWS'


def test_a_duplicated_filter_disables_itself_and_redirects(app, db_session):
    """The route's own comment: jinja is picky about the python it will run, so
    a filter appearing TWICE means "turn this off". The tidy-up then redirects
    to a url without it, which is what this row asserts -- the redirect is the
    only way to see that the cleanup happened.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered'):
        response = client.get('/instances?filters=trusted&filters=trusted')

    assert response.status_code == 302
    assert 'filters' not in response.headers['Location']


def test_a_filter_given_once_is_kept(app, db_session):
    """The other arm of the de-duplication, and the row that stops it removing
    everything: one occurrence survives and the page renders rather than
    redirecting.
    """
    instance, alice, bob = _seed()
    _instance('trusted.example', trusted=True)
    _instance('ordinary.example', trusted=False)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instances?filters=trusted')

    assert response.status_code == 200
    assert _domains(render) == ['trusted.example']


@pytest.mark.parametrize('filter_name, wanted', [
    ('trusted', 'trusted.example'),
    ('silenced', 'silenced.example'),
    ('online', 'online.example'),
    ('dormant', 'dormant.example'),
    ('gone_forever', 'gone.example'),
])
def test_each_state_filter_narrows_the_list(app, db_session, filter_name, wanted):
    """Five filters, each its own row, against one fixture that holds an
    instance in every state -- so a filter that stopped working returns the
    others rather than nothing.
    """
    instance, alice, bob = _seed()
    _instance('trusted.example', trusted=True, dormant=True, gone_forever=True)
    _instance('silenced.example', silenced=True, dormant=True, gone_forever=True)
    _instance('online.example', dormant=False, gone_forever=False)
    _instance('dormant.example', dormant=True, gone_forever=False)
    _instance('gone.example', dormant=False, gone_forever=True)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get(f'/instances?filters={filter_name}')

    assert wanted in _domains(render)


def test_the_federated_filter_excludes_this_server(app, db_session):
    """`Instance.id != 1` -- the local instance is always id 1, so "federated"
    means everyone else. The row that proves it is the local instance being
    absent while an online peer is present.
    """
    instance, alice, bob = _seed()
    _instance('peer.example', dormant=False, gone_forever=False)
    _instance('gone.example', gone_forever=True)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances?filters=federated')

    assert _domains(render) == ['peer.example']


def test_two_state_filters_narrow_together(app, db_session):
    """The filters are applied in sequence rather than as alternatives, so
    asking for two means both -- and the fixture has an instance matching each
    one alone, which a mutant turning the sequence into a disjunction would
    return.
    """
    instance, alice, bob = _seed()
    _instance('both.example', trusted=True, silenced=True)
    _instance('trusted.example', trusted=True, silenced=False)
    _instance('silenced.example', trusted=False, silenced=True)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances?filters=trusted&filters=silenced')

    assert _domains(render) == ['both.example']


# --------------------------------------------------------------------------
# list_instances: the allowed and blocked lists
# --------------------------------------------------------------------------


def test_the_allowed_filter_lists_the_allowlist_instead(app, db_session):
    """`allowed` swaps the whole query for a different TABLE, so the rows that
    come back are AllowedInstances rather than Instance -- and an Instance with
    the same name must not appear.
    """
    instance, alice, bob = _seed()
    _instance('peer.example')
    db.session.add(AllowedInstances(domain='allowed.example'))
    db.session.commit()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances?filters=allowed')

    assert _domains(render) == ['allowed.example']
    assert render.call_args.kwargs['allowed_or_blocked'] is True


def test_the_blocked_filter_lists_the_banlist_instead(app, db_session):
    instance, alice, bob = _seed()
    db.session.add(BannedInstances(domain='banned.example'))
    db.session.commit()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances?filters=blocked')

    assert _domains(render) == ['banned.example']


@pytest.mark.parametrize('mode', ['allowed', 'blocked'])
def test_those_lists_can_be_searched_too(app, db_session, mode):
    """Each mode has its own search clause against its own table, so both need
    a row -- and both are ilike, so both are asked in the wrong case.
    """
    instance, alice, bob = _seed()
    model = AllowedInstances if mode == 'allowed' else BannedInstances
    db.session.add(model(domain='news.example'))
    db.session.add(model(domain='sport.example'))
    db.session.commit()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get(f'/instances?filters={mode}&search=NEWS')

    assert _domains(render) == ['news.example']


@pytest.mark.parametrize('mode', ['allowed', 'blocked'])
def test_a_state_filter_beside_allowed_or_blocked_is_dropped_with_a_warning(app, db_session, mode):
    """The state filters read columns the allowlist and banlist tables do not
    have, so the route strips them and redirects -- and flashes to say why.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered'), \
         patch('app.instance.routes.flash') as flashed:
        response = client.get(f'/instances?filters={mode}&filters=trusted')

    assert response.status_code == 302
    assert f'filters={mode}' in response.headers['Location']
    assert 'trusted' not in response.headers['Location']
    assert flashed.call_count == 1
    assert 'disables filters other than search' in str(flashed.call_args.args[0])


def test_asking_for_allowed_and_blocked_at_once_gives_nothing(app, db_session):
    """Deliberately an empty query rather than a redirect, per the route's own
    comment -- an instance cannot be on both lists, and returning the union
    would break the pagination below.
    """
    instance, alice, bob = _seed()
    db.session.add(AllowedInstances(domain='allowed.example'))
    db.session.add(BannedInstances(domain='banned.example'))
    db.session.commit()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instances?filters=allowed&filters=blocked')

    assert response.status_code == 200
    assert _domains(render) == []


def test_the_page_says_which_lists_have_anything_in_them(app, db_session):
    """Four counters drive four buttons in the template, and each is its own
    query -- so the fixture turns them on one at a time.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances')
        empty = render.call_args.kwargs
        assert (empty['allowed'], empty['blocked'], empty['trusted'], empty['silenced']) == \
            (False, False, False, False)

        db.session.add(AllowedInstances(domain='allowed.example'))
        db.session.add(BannedInstances(domain='banned.example'))
        _instance('trusted.example', trusted=True)
        _instance('silenced.example', silenced=True)
        client.get('/instances')
        full = render.call_args.kwargs
        assert (full['allowed'], full['blocked'], full['trusted'], full['silenced']) == \
            (True, True, True, True)


def test_the_instance_list_paginates(app, db_session):
    instance, alice, bob = _seed()
    for index in range(51):
        _instance(f'peer{index:03d}.example')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances')
        assert render.call_args.kwargs['next_url'] is not None
        assert render.call_args.kwargs['prev_url'] is None
        client.get('/instances?page=2')
        assert render.call_args.kwargs['next_url'] is None
        assert render.call_args.kwargs['prev_url'] is not None


def test_a_low_bandwidth_reader_is_told_so(app, db_session):
    """The cookie is passed straight to the template, and it needs the app's
    SERVER_NAME as its domain to arrive at all (fact 314).
    """
    instance, alice, bob = _seed()
    client = app.test_client()
    client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instances')

    assert render.call_args.kwargs['low_bandwidth'] is True


# --------------------------------------------------------------------------
# instance_overview, instance_people, instance_people_top
# --------------------------------------------------------------------------


def test_the_overview_finds_an_instance_by_domain(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instance/peer.example')

    assert response.status_code == 200
    assert render.call_args.kwargs['instance'].id == peer.id
    assert client.get('/instance/nosuch.example').status_code == 404


def _person(instance, name, **kwargs):
    user = make_user(instance, name, local=False)
    user.searchable = True
    user.bot = False
    user.bot_override = False
    for key, value in kwargs.items():
        setattr(user, key, value)
    db.session.commit()
    return user


def _names(render):
    return sorted(u.user_name for u in render.call_args.kwargs['people'].items)


def test_the_people_page_lists_an_instances_users(app, db_session):
    """Bots, deleted and banned accounts, and anyone who has asked not to be
    listed are all excluded -- four exclusions, so four rows in the fixture
    plus the one that survives.
    """
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    _person(peer, 'visible')
    _person(peer, 'deleted', deleted=True)
    _person(peer, 'banned', banned=True)
    _person(peer, 'robot', bot=True)
    _person(peer, 'private', searchable=False)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instance/peer.example/people')

    assert response.status_code == 200
    assert [u.user_name for u in render.call_args.kwargs['people'].items] == ['visible']
    assert render.call_args.kwargs['instance'].id == peer.id


def test_an_admin_sees_people_who_asked_not_to_be_listed(app, db_session):
    """The admin branch differs from the other in exactly one filter --
    `searchable=True` -- and this is the row that shows it.
    """
    instance, alice, bob = _seed()
    _make_admin(alice)
    peer = _instance('peer.example')
    _person(peer, 'visible')
    _person(peer, 'private', searchable=False)
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/people')

    assert len(render.call_args.kwargs['people'].items) == 2


def test_people_from_a_silenced_instance_are_left_out(app, db_session):
    """The silenced filter keeps users with NO instance as well, which is what
    the `or_` is for -- so the fixture has one of those too.
    """
    instance, alice, bob = _seed()
    silenced = _instance('silenced.example', silenced=True)
    peer = _instance('peer.example')
    _person(silenced, 'quiet')
    _person(peer, 'loud')
    orphan = _person(peer, 'orphan')
    orphan.instance_id = None
    db.session.commit()
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/all/people')

    names = [u.user_name for u in render.call_args.kwargs['people'].items]
    assert 'quiet' not in names
    assert 'loud' in names
    assert 'orphan' in names


def test_the_all_and_local_people_pages_choose_their_own_scope(app, db_session):
    """`all` leaves the instance filter off entirely; `local` pins it to
    instance 1. The titles differ too, and the assertion reads the instance the
    template is given rather than the title, which is translated.
    """
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    _person(peer, 'remote')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/all/people')
        assert render.call_args.kwargs['instance'] is None
        # burnseat counts: it is an ordinary local user, not deleted or a bot
        assert len(render.call_args.kwargs['people'].items) == 4

        client.get('/instance/local/people')
        assert render.call_args.kwargs['instance'].id == 1
        assert sorted(u.user_name for u in render.call_args.kwargs['people'].items) == \
            ['alice', 'bob', 'burnseat']


def test_the_people_page_can_be_searched(app, db_session):
    """A full-text search over users, which nothing in this suite could run
    before sub-project 62 installed the search SQL (fact 317).
    """
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    _person(peer, 'gardener')
    _person(peer, 'mechanic')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/people?q=gardener')

    assert [u.user_name for u in render.call_args.kwargs['people'].items] == ['gardener']
    assert render.call_args.kwargs['q'] == 'gardener'


def test_the_people_page_paginates(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    for index in range(51):
        _person(peer, f'person{index:03d}')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/people')
        assert render.call_args.kwargs['people'].per_page == 50
        assert render.call_args.kwargs['next_url'] is not None
        client.get('/instance/peer.example/people?page=2')
        assert render.call_args.kwargs['prev_url'] is not None


def test_a_logged_in_reader_gets_a_hundred_people_a_page(app, db_session):
    """Its own test: a manual login does not take on a client that has already
    made an anonymous request in the same test (fact 315)."""
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    _person(peer, 'somebody')
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/people')

    assert render.call_args.kwargs['people'].per_page == 100


def test_the_interesting_people_page_wants_active_accounts_with_faces(app, db_session):
    """Five conditions in one filter -- posts, replies, reputation, a recent
    visit and an avatar -- so the fixture fails one at a time.
    """
    from datetime import timedelta
    from app.models import File
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    avatar = File(file_path='avatar.png')
    db.session.add(avatar)
    db.session.commit()
    good = dict(post_count=5, post_reply_count=5, reputation=5,
                last_seen=utcnow(), avatar_id=avatar.id)
    _person(peer, 'interesting', **good)
    _person(peer, 'noposts', **{**good, 'post_count': 1})
    _person(peer, 'noreplies', **{**good, 'post_reply_count': 1})
    _person(peer, 'norep', **{**good, 'reputation': 1})
    _person(peer, 'stale', **{**good, 'last_seen': utcnow() - timedelta(days=5)})
    _person(peer, 'faceless', **{**good, 'avatar_id': None})
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instance/people/interesting')

    assert response.status_code == 200
    assert [u.user_name for u in render.call_args.kwargs['people'].items] == ['interesting']
    assert render.call_args.kwargs['instance'] is None


def test_the_interesting_people_page_can_be_searched_and_paginated(app, db_session):
    from app.models import File
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    silenced = _instance('silenced.example', silenced=True)
    avatar = File(file_path='avatar.png')
    db.session.add(avatar)
    db.session.commit()
    good = dict(post_count=5, post_reply_count=5, reputation=5,
                last_seen=utcnow(), avatar_id=avatar.id)
    _person(peer, 'gardener', **good)
    _person(peer, 'mechanic', **good)
    _person(silenced, 'quiet', **good)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/people/interesting?q=gardener')
        assert [u.user_name for u in render.call_args.kwargs['people'].items] == ['gardener']

        client.get('/instance/people/interesting')
        names = [u.user_name for u in render.call_args.kwargs['people'].items]
        assert 'quiet' not in names
        assert render.call_args.kwargs['next_url'] is None
        assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# instance_posts
# --------------------------------------------------------------------------


def _post_from(instance, community, author, title, **kwargs):
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    post.instance_id = instance.id
    post.status = POST_STATUS_REVIEWING + 1
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.commit()
    return post


def _post_titles(render):
    return sorted(p.title for p in render.call_args.kwargs['posts'].items)


def test_the_posts_page_shows_that_instances_posts(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    other = _instance('other.example')
    community = make_community('microblogs')
    _post_from(peer, community, alice, 'theirs')
    _post_from(other, community, alice, 'elsewhere')
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instance/peer.example/posts')

    assert response.status_code == 200
    assert _post_titles(render) == ['theirs']
    assert render.call_args.kwargs['instance'].id == peer.id
    assert client.get('/instance/nosuch.example/posts').status_code == 404


def test_an_anonymous_reader_sees_no_bots_nsfw_nsfl_or_deleted_posts(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    community = make_community('microblogs')
    _post_from(peer, community, alice, 'plain')
    _post_from(peer, community, alice, 'bot', from_bot=True)
    _post_from(peer, community, alice, 'spicy', nsfw=True)
    _post_from(peer, community, alice, 'grim', nsfl=True)
    _post_from(peer, community, alice, 'gone', deleted=True)
    _post_from(peer, community, alice, 'pending', status=POST_STATUS_REVIEWING)
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')

    assert _post_titles(render) == ['plain']
    assert render.call_args.kwargs['content_filters'] == {}


def test_a_logged_in_readers_own_preferences_decide_what_they_see(app, db_session):
    """The authenticated arm reads four preferences, each its own guard, so the
    fixture turns them all on and supplies a post per exclusion.
    """
    from tests.factories import mark_post_read
    instance, alice, bob = _seed()
    alice.ignore_bots = 1
    alice.hide_nsfl = 1
    alice.hide_nsfw = 1
    alice.hide_read_posts = True
    db.session.commit()
    peer = _instance('peer.example')
    community = make_community('microblogs')
    _post_from(peer, community, alice, 'plain')
    _post_from(peer, community, alice, 'bot', from_bot=True)
    _post_from(peer, community, alice, 'spicy', nsfw=True)
    _post_from(peer, community, alice, 'grim', nsfl=True)
    seen = _post_from(peer, community, alice, 'already read')
    mark_post_read(alice, seen)
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')

    assert _post_titles(render) == ['plain']


def test_a_logged_in_reader_who_hides_nothing_sees_everything(app, db_session):
    """The other side of all four guards, which is what stops any of them being
    load-bearing for nothing.
    """
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    alice.hide_nsfl = 0
    alice.hide_nsfw = 0
    alice.hide_read_posts = False
    db.session.commit()
    peer = _instance('peer.example')
    community = make_community('microblogs')
    _post_from(peer, community, alice, 'plain')
    _post_from(peer, community, alice, 'bot', from_bot=True)
    _post_from(peer, community, alice, 'spicy', nsfw=True)
    _post_from(peer, community, alice, 'grim', nsfl=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')

    assert _post_titles(render) == ['bot', 'grim', 'plain', 'spicy']


def test_a_logged_in_readers_block_lists_apply(app, db_session):
    """Four block lists, each conditional on being non-empty: domains,
    instances, communities and users.
    """
    from tests.factories import (make_community_block, make_domain, make_domain_block,
                                 make_instance_block, make_user_block)
    instance, alice, bob = _seed()
    alice.ignore_bots = 0
    peer = _instance('peer.example')
    blocked_instance = _instance('blocked.example')
    community = make_community('microblogs')
    blocked_community = make_community('blockedcomm')
    domain = make_domain('blocked.test')
    _post_from(peer, community, alice, 'plain')
    _post_from(peer, community, alice, 'domain', domain_id=domain.id)
    _post_from(peer, blocked_community, alice, 'community')
    _post_from(peer, community, bob, 'author')
    make_domain_block(alice, domain)
    make_instance_block(alice, blocked_instance)
    make_community_block(alice, blocked_community)
    make_user_block(alice, bob)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')

    assert _post_titles(render) == ['plain']


def test_the_posts_page_carries_breadcrumbs_and_voting_history(app, db_session):
    from tests.factories import make_post_vote
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    community = make_community('microblogs')
    post = _post_from(peer, community, alice, 'plain')
    make_post_vote(alice, post, 1)
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')

    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [c.url for c in crumbs] == ['/', '/instances', '/instance/peer.example']
    assert post.id in render.call_args.kwargs['recently_upvoted']
    assert render.call_args.kwargs['recently_downvoted'] == []


def test_the_posts_page_paginates_and_sorts_newest_first(app, db_session):
    from datetime import timedelta
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    community = make_community('microblogs')
    older = _post_from(peer, community, alice, 'older',
                       posted_at=utcnow() - timedelta(days=2))
    newer = _post_from(peer, community, alice, 'newer')
    for index in range(50):
        _post_from(peer, community, alice, f'filler {index}',
                   posted_at=utcnow() - timedelta(days=5))
    client = app.test_client()

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/peer.example/posts')
        assert [p.title for p in render.call_args.kwargs['posts'].items][:2] == \
            ['newer', 'older']
        assert render.call_args.kwargs['next_url'] is not None
        client.get('/instance/peer.example/posts?page=2')
        assert render.call_args.kwargs['prev_url'] is not None


# --------------------------------------------------------------------------
# instance_add_people
# --------------------------------------------------------------------------


def test_the_add_people_form_renders_with_the_referrer_filled_in(app, db_session):
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        response = client.get('/instance/add_people',
                              headers={'Referer': 'https://test.piefed.local/instances'})

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].referrer.data == 'https://test.piefed.local/instances'


def test_a_banned_user_cannot_add_people(app, db_session):
    instance, alice, bob = _seed()
    _submitter(alice)
    alice.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered'):
        response = client.get('/instance/add_people')

    assert response.status_code == 302


def test_typed_handles_are_followed_and_anything_else_is_ignored(app, db_session):
    """The textarea is split on newlines and every line is checked with
    is_fedi_handle, so the fixture mixes real handles with a blank line and a
    line of prose -- and only the handles reach the follower.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.instance.routes.render_template', return_value='rendered'), \
         patch('app.instance.routes.bulk_follow') as follower, \
         patch('app.instance.routes.flash') as flashed:
        response = client.post('/instance/add_people',
                               data={'csrf_token': token,
                                     'people': '@alice@example.test\nnot a handle\n\n'
                                               '  @bob@other.example  '})

    assert response.status_code == 302
    assert follower.delay.call_args.args[1] == ['@alice@example.test', '@bob@other.example']
    assert flashed.call_count == 1


def test_a_mastodon_csv_is_read_for_handles(app, db_session):
    """The export's first column is the handle; rows whose first column is not
    one are skipped, which the header row exercises for free.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)
    csv_text = ('Account address,Show boosts\n'
                '@alice@example.test,true\n'
                '@bob@other.example,false\n'
                '\n')

    with patch('app.instance.routes.render_template', return_value='rendered'), \
         patch('app.instance.routes.bulk_follow') as follower:
        client.post('/instance/add_people',
                    data={'csrf_token': token,
                          'mastodon_csv': (io.BytesIO(csv_text.encode('utf-8')), 'follows.csv')},
                    content_type='multipart/form-data')

    assert follower.delay.call_args.args[1] == ['@alice@example.test', '@bob@other.example']


def test_a_debug_server_follows_in_process(app, db_session):
    """`if current_app.debug` -- the whole body of that branch is the choice
    between a direct call and a task, so both arms are rows.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    app.debug = True
    try:
        with patch('app.instance.routes.render_template', return_value='rendered'), \
             patch('app.instance.routes.bulk_follow') as follower:
            client.post('/instance/add_people',
                        data={'csrf_token': token, 'people': '@alice@example.test'})
    finally:
        app.debug = False

    assert follower.call_count == 1
    assert follower.delay.call_count == 0


def test_a_csv_that_is_not_utf8_is_a_500(app, db_session):
    """D803, recorded rather than repaired: the upload is decoded as UTF-8 with
    no guard, so any other encoding is a traceback. A Mastodon export is always
    UTF-8, which is why this has survived -- but the answer to the wrong file
    is a message, and what that message should say is a product decision.
    """
    instance, alice, bob = _seed()
    _submitter(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.instance.routes.render_template', return_value='rendered'), \
         patch('app.instance.routes.bulk_follow'):
        with pytest.raises(UnicodeDecodeError):
            client.post('/instance/add_people',
                        data={'csrf_token': token,
                              'mastodon_csv': (io.BytesIO(b'\xff\xfe@alice@example.test'),
                                               'follows.csv')},
                        content_type='multipart/form-data')


# --------------------------------------------------------------------------
# instance_block
# --------------------------------------------------------------------------


def test_blocking_an_instance_records_it_and_redirects(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/block', data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/user/settings/filters'
    block = InstanceBlock.query.one()
    assert block.user_id == alice.id
    assert block.instance_id == peer.id


def test_blocking_over_htmx_answers_with_a_redirect_header(app, db_session):
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/block', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/instance/{peer.domain}'


def test_the_block_pair_honours_a_safe_redirect_target(app, db_session):
    """`safe_redirect_target` is what stops `?redirect=` sending the reader to
    another site, so both rows here are about it: a local path is honoured and
    an absolute url elsewhere is not.
    """
    instance, alice, bob = _seed()
    peer = _instance('peer.example')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    local = client.post(f'/instance/{peer.id}/block?redirect=/instances',
                        data={'csrf_token': token})
    assert local.headers['Location'] == '/instances'

    away = client.post(f'/instance/{peer.id}/unblock?redirect=https://evil.example/',
                       data={'csrf_token': token})
    assert away.headers['Location'] == '/user/settings/filters'


@pytest.mark.parametrize('path', ['block', 'unblock'])
def test_blocking_an_unknown_instance_is_a_404(app, db_session, path):
    instance, alice, bob = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    assert client.post(f'/instance/9999/{path}',
                       data={'csrf_token': token}).status_code == 404


def test_the_people_page_of_an_unknown_instance_is_a_404(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    assert client.get('/instance/nosuch.example/people').status_code == 404


def test_an_admin_also_loses_people_from_silenced_instances(app, db_session):
    """The admin branch repeats the silenced filter rather than sharing it, so
    it needs its own row -- and the admin's extra reach does not extend to a
    silenced instance's users.
    """
    instance, alice, bob = _seed()
    _make_admin(alice)
    silenced = _instance('silenced.example', silenced=True)
    peer = _instance('peer.example')
    _person(silenced, 'quiet')
    _person(peer, 'loud')
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/all/people')

    names = [u.user_name for u in render.call_args.kwargs['people'].items]
    assert 'quiet' not in names
    assert 'loud' in names


def test_an_admin_asking_for_everyone_gets_no_instance_filter(app, db_session):
    """`if instance:` in the admin branch, false arm -- /all/people leaves the
    instance None and the query unscoped.
    """
    instance, alice, bob = _seed()
    _make_admin(alice)
    peer = _instance('peer.example')
    _person(peer, 'remote')
    client = app.test_client()
    login(client, alice)

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        client.get('/instance/all/people')

    assert render.call_args.kwargs['instance'] is None
    names = [u.user_name for u in render.call_args.kwargs['people'].items]
    assert 'remote' in names and 'alice' in names


def test_the_warning_about_disabled_filters_always_names_one_of_the_two(app, db_session):
    """THE MODULE'S ONE RESIDUAL ARC, PROVED.

    Inside `if allowed_or_blocked:` the route flashes for 'blocked' and, failing
    that, for 'allowed'. The arc where NEITHER matches -- falling past both to
    the redirect below -- cannot run: `allowed_or_blocked` is assigned from

        if 'allowed' in filters or 'blocked' in filters

    and nothing between that assignment and the flash removes either string
    from `filters`. The loop above it removes only the names in
    `filters_to_remove`, which are the six state filters.

    Asserted on the list rather than through a request, since no query string
    can reach the arc.
    """
    filters_to_remove = ['online', 'trusted', 'silenced', 'dormant', 'gone_forever',
                         'federated']

    assert 'allowed' not in filters_to_remove
    assert 'blocked' not in filters_to_remove
