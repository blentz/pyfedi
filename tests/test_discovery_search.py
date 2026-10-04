"""Interop D24, goal (b): when nothing local matches, community search and people search offer what the
discovery directory knows, with a platform badge and a button that resolves the actor."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.discovery import search as search_mod, views
from app.discovery.search import discovery_fallback, viewer_allows_nsfw
from app.models import Domain, InstanceBlock, Site
from tests.discovery_fixtures import add_entry as shared_add_entry, fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('fresh_cache')


def add_entry(name, kind='community', platform='peertube', nsfw=False, host='tube.example', followers=1):
    """The shared helper, with a fixed default host and a url that is safe for any name."""
    path = 'video-channels' if kind == 'community' else 'users'
    slug = name.lower().replace(' ', '_').replace('%', 'pct')
    return shared_add_entry(name, followers=followers, platform=platform, kind=kind, nsfw=nsfw, host=host,
                            url=f'https://{host}/{path}/{slug}')


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user1
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return SimpleNamespace(app=app, client=client, user=user, token=csrf(app, client))


def test_the_fallback_matches_names_case_insensitively_and_by_kind(app, db_session):
    add_entry('ZqTilvids Linux')
    add_entry('Ann Zqtilvids', kind='person', platform='mastodon', host='m.example')

    assert [e.name for e in discovery_fallback('community', 'zqtilvids', False)] == ['ZqTilvids Linux']
    assert [e.name for e in discovery_fallback('person', 'ZQTILVIDS', False)] == ['Ann Zqtilvids']
    assert discovery_fallback('community', '   ', True) == []


def test_nsfw_entries_need_the_viewers_permission(app, db_session):
    add_entry('Zqspicy', nsfw=True)

    assert discovery_fallback('community', 'zqspicy', False) == []
    assert [e.name for e in discovery_fallback('community', 'zqspicy', True)] == ['Zqspicy']


def test_entries_on_a_host_banned_since_the_refresh_are_hidden(app, db_session):
    add_entry('Zqbanned', host='banned.example')
    make_banned_instance('banned.example')

    assert discovery_fallback('community', 'zqbanned', True) == []


def test_like_wildcards_in_the_search_text_match_literally(app, db_session):
    """Review focus 3."""
    add_entry('100% Linux')
    add_entry('1000 Linux')
    add_entry('a_b')
    add_entry('axb')

    assert [e.name for e in discovery_fallback('community', '100%', True)] == ['100% Linux']
    assert [e.name for e in discovery_fallback('community', 'a_b', True)] == ['a_b']


def test_viewer_allows_nsfw(app, db_session):
    site = SimpleNamespace(enable_nsfw=True)
    anonymous = SimpleNamespace(is_authenticated=False)
    shown = SimpleNamespace(is_authenticated=True, hide_nsfw=0)
    hidden = SimpleNamespace(is_authenticated=True, hide_nsfw=1)

    assert viewer_allows_nsfw(shown, site) is True
    assert viewer_allows_nsfw(hidden, site) is False
    assert viewer_allows_nsfw(anonymous, site) is False
    assert viewer_allows_nsfw(shown, SimpleNamespace(enable_nsfw=False)) is False


def test_the_communities_page_offers_the_directory_when_nothing_local_matches(env):
    entry = add_entry('Zqtilvids Linux')

    html = env.client.get('/communities?search=zqtilvids').get_data(as_text=True)

    assert 'Zqtilvids Linux' in html
    assert 'PeerTube' in html
    assert f'/discovery/{entry.id}/resolve' in html


def test_a_name_with_html_entities_is_escaped_on_render(env):
    add_entry('Zqent &amp; &lt;script&gt;alert(1)&lt;/script&gt;')

    html = env.client.get('/communities?search=zqent').get_data(as_text=True)

    assert 'Zqent &amp;amp; &amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;' in html
    assert '<script>alert(1)' not in html


def test_a_local_match_means_no_fallback(env):
    add_entry('Zqtilvids Linux')
    community = make_community('zqtilvids')
    community.title = 'Zqtilvids'
    db.session.commit()

    with patch('app.main.routes.render_template', return_value='rendered') as render:
        env.client.get('/communities?search=zqtilvids')

    assert render.call_args.kwargs['discovered'] == []


def test_nsfw_entries_are_hidden_on_the_communities_page_by_default(env):
    add_entry('Zqspicy', nsfw=True)

    with patch('app.main.routes.render_template', return_value='rendered') as render:
        env.client.get('/communities?search=zqspicy')

    assert render.call_args.kwargs['discovered'] == []


def test_people_search_offers_directory_people(env):
    add_entry('Zqann Example', kind='person', platform='mastodon', host='m.example')

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        env.client.get('/instance/all/people?q=zqann')

    assert [e.name for e in render.call_args.kwargs['discovered']] == ['Zqann Example']


def test_the_people_page_renders_a_follow_button_for_a_directory_person(env):
    entry = add_entry('Zqann Example', kind='person', platform='pixelfed', host='px.example')

    html = env.client.get('/instance/all/people?q=zqann').get_data(as_text=True)

    assert 'Zqann Example' in html and 'Pixelfed' in html
    assert f'/discovery/{entry.id}/resolve' in html


def test_resolve_finds_the_community_and_redirects_to_it(env, monkeypatch):
    entry = add_entry('Zqtilvids Linux')
    community = make_community('zqtilvids_linux', host='tube.example')
    community.ap_id = 'zqtilvids_linux@tube.example'
    db.session.commit()
    calls = []
    monkeypatch.setattr(views, 'find_actor_or_create',
                        lambda actor_url, community_only=False: calls.append((actor_url, community_only)) or community)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/c/{community.link()}')
    assert calls == [(entry.actor_url, True)]


def test_resolve_finds_a_person_and_redirects_to_their_profile(env, monkeypatch):
    entry = add_entry('Zqann Example', kind='person', platform='mastodon', host='m.example')
    person = make_user(make_instance('m.example'), 'zqann')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: person)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.headers['Location'].endswith(f'/u/{person.link()}')


def test_an_actor_that_cannot_be_reached_sends_the_viewer_back(env, monkeypatch):
    entry = add_entry('Zqgone')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: None)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/communities')


def test_resolving_needs_a_login(app, api_baseline, monkeypatch):
    entry = add_entry('Zqtilvids Linux')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda *a, **k: pytest.fail('anonymous must not resolve'))

    response = app.test_client().post(f'/discovery/{entry.id}/resolve')

    assert response.status_code in (302, 401)


def test_resolve_is_post_only_and_only_for_known_entries(env, monkeypatch):
    entry = add_entry('Zqtilvids Linux')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda *a, **k: pytest.fail('must not resolve'))

    assert env.client.get(f'/discovery/{entry.id}/resolve').status_code == 405
    assert env.client.post('/discovery/999999/resolve', data={'csrf_token': env.token}).status_code == 404
    assert env.client.post(f'/discovery/{entry.id}/resolve').status_code == 400


# ---- M1: the viewer's own blocks and the full instance filter apply to the fallback ----------------------------

def test_a_host_the_viewer_blocked_is_hidden_from_them_only(app, db_session):
    make_instance('local.example')   # instance 1, which a local user belongs to
    blocker = make_user(None, 'blocker', local=True)
    add_entry('Zqblocked', host='blocked.example')
    blocked = make_instance('blocked.example')
    db.session.add(InstanceBlock(user_id=blocker.id, instance_id=blocked.id))
    db.session.commit()

    assert discovery_fallback('community', 'zqblocked', True, viewer_id=blocker.id) == []
    assert [e.name for e in discovery_fallback('community', 'zqblocked', True)] == ['Zqblocked']


def test_a_banned_domain_is_hidden(app, db_session):
    add_entry('Zqdomain', host='bad.example')
    db.session.add(Domain(name='bad.example', banned=True))
    db.session.commit()

    assert discovery_fallback('community', 'zqdomain', True) == []


def test_allowlist_mode_hides_hosts_off_the_list(app, db_session, monkeypatch):
    add_entry('Zqallowed', host='allowed.example')
    add_entry('Zqnotallowed', host='other.example')
    monkeypatch.setattr(search_mod, 'host_is_excluded', lambda host, isolated: host != 'allowed.example')

    assert [e.name for e in discovery_fallback('community', 'zqallowed', True)] == ['Zqallowed']
    assert discovery_fallback('community', 'zqnotallowed', True) == []


def test_the_communities_page_passes_the_viewer(env):
    add_entry('Zqblocked Linux', host='blocked.example')
    db.session.add(InstanceBlock(user_id=env.user.id, instance_id=make_instance('blocked.example').id))
    db.session.commit()

    with patch('app.main.routes.render_template', return_value='rendered') as render:
        env.client.get('/communities?search=zqblocked')

    assert render.call_args.kwargs['discovered'] == []


# ---- M2: an instance's people page offers only that instance's directory people ---------------------------------

def test_an_instance_people_page_offers_only_that_instances_directory_people(env):
    make_instance('m.example')
    add_entry('Zqann Here', kind='person', platform='mastodon', host='m.example')
    add_entry('Zqann Elsewhere', kind='person', platform='mastodon', host='n.example')

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        env.client.get('/instance/m.example/people?q=zqann')

    assert [e.name for e in render.call_args.kwargs['discovered']] == ['Zqann Here']


# ---- C3: exclusions do not under-fill the results -----------------------------------------------------------------

def test_excluded_hosts_do_not_crowd_out_the_limit(app, db_session):
    for n in range(3):
        add_entry(f'Zqcrowd banned {n}', host='banned.example', followers=100 + n)
    add_entry('Zqcrowd fine a', host='a.example', followers=50)
    add_entry('Zqcrowd fine b', host='b.example', followers=40)
    make_banned_instance('banned.example')

    assert [e.name for e in discovery_fallback('community', 'zqcrowd', True, limit=2)] == \
        ['Zqcrowd fine a', 'Zqcrowd fine b']


def test_the_fallback_reads_at_most_five_pages(app, db_session):
    for n in range(10):   # five pages of two rows each, with limit=1
        add_entry(f'Zqdeep banned {n}', host='banned.example', followers=100 + n)
    add_entry('Zqdeep fine', host='a.example', followers=1)
    make_banned_instance('banned.example')

    assert discovery_fallback('community', 'zqdeep', True, limit=1) == []
    assert [e.name for e in discovery_fallback('community', 'zqdeep', True, limit=2)] == ['Zqdeep fine']


def test_resolving_an_entry_on_a_host_banned_after_the_refresh_is_refused(env, monkeypatch):
    entry = add_entry('Zqbanned', host='banned.example')
    make_banned_instance('banned.example')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda *a, **k: pytest.fail('a banned host must not be fetched'))

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token, 'q': 'zqbanned'})

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/communities?search=zqbanned')


def test_the_failure_redirect_keeps_the_search(env, monkeypatch):
    community = add_entry('Zqgone')
    person = add_entry('Zqann Gone', kind='person', platform='mastodon', host='m.example')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: None)

    to_communities = env.client.post(f'/discovery/{community.id}/resolve', data={'csrf_token': env.token, 'q': 'zqgone'})
    to_people = env.client.post(f'/discovery/{person.id}/resolve', data={'csrf_token': env.token, 'q': 'zqann'})

    assert to_communities.headers['Location'].endswith('/communities?search=zqgone')
    assert to_people.headers['Location'].endswith('/instance/all/people?q=zqann')


def test_the_resolve_form_carries_the_search(env):
    add_entry('Zqtilvids Linux')
    add_entry('Zqann Example', kind='person', platform='mastodon', host='m.example')

    communities = env.client.get('/communities?search=zqtilvids').get_data(as_text=True)
    people = env.client.get('/instance/all/people?q=zqann').get_data(as_text=True)

    assert '<input type="hidden" name="q" value="zqtilvids">' in communities
    assert '<input type="hidden" name="q" value="zqann">' in people
