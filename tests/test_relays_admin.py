"""Task 6 (spec: Admin and CLI): the admin relays page and the `flask relays` commands."""
from datetime import timedelta

import pytest

import app.relays.admin_views as views
import app.cli as cli
import app.relays.cli as relay_cli
from app import db
from app.constants import ALLOWLIST_STRONG
from app.models import Relay, Site, utcnow
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, STYLE_MASTODON
from app.relays.subscribe import RelayError
from app.utils import get_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_admin_federation import csrf, login

pytestmark = [pytest.mark.usefixtures('site', 'fresh_cache')]

PAGE = '/admin/federation/relays'


def make_relay(url='https://relay.example/inbox', state=RELAY_ACCEPTED):
    relay = Relay(url=url, style=STYLE_MASTODON, inbox_url=url, state=state,
                  follow_activity_id='https://test.piefed.local/activities/relay-follow/x')
    db.session.add(relay)
    db.session.commit()
    return relay


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(views, 'add_relay', lambda url: seen.append(('add', url)) or make_relay(url))
    monkeypatch.setattr(views, 'retry_relay', lambda relay: seen.append(('retry', relay.id)))
    monkeypatch.setattr(views, 'remove_relay', lambda relay: seen.append(('remove', relay.id)))
    return seen


def test_anonymous_is_redirected_to_login(app):
    response = app.test_client().get(PAGE)

    assert response.status_code == 302 and 'login' in response.headers['Location']


def test_a_non_admin_is_refused(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)   # user 1 is an administrator
    user = make_user(instance, 'plain', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)

    response = client.get(PAGE)

    assert response.status_code == 302 and 'permission' in response.headers['Location']


def test_get_lists_relays_with_the_24_hour_post_count(admin):
    client, _ = admin
    relay = make_relay()
    community = make_community('microblogs')
    author = make_user(make_instance('remote.example'), 'author')
    for n, age in enumerate((timedelta(hours=1), timedelta(hours=2), timedelta(hours=30))):
        post = make_post(community, author, f'https://remote.example/notes/{n}', microblog=True)
        post.created_at = utcnow() - age
        post.relay_id = relay.id
    db.session.commit()

    page = client.get(PAGE).get_data(as_text=True)

    assert 'https://relay.example/inbox' in page and STYLE_MASTODON in page and RELAY_ACCEPTED in page
    assert '<td class="relay_posts">2</td>' in page
    assert 'confirm_first' in page


def test_get_shows_the_allowlist_note_only_in_strong_mode(admin):
    client, _ = admin
    assert 'allowlisted servers' not in client.get(PAGE).get_data(as_text=True)

    db.session.get(Site, 1).allowlist_mode = ALLOWLIST_STRONG
    db.session.commit()

    page = client.get(PAGE).get_data(as_text=True)
    assert 'allowlisted servers' in page
    assert 'LitePub' in page and 'author' in page


def test_add_calls_add_relay_and_redirects(admin, calls):
    client, token = admin

    response = client.post(PAGE, data={'relay_url': 'https://relay.example/inbox', 'relay_add': 'go',
                                       'csrf_token': token})

    assert response.status_code == 302 and calls == [('add', 'https://relay.example/inbox')]
    assert 'Subscribed to' in client.get(PAGE).get_data(as_text=True)


def test_add_with_an_invalid_url_does_not_call_add_relay(admin, calls):
    client, token = admin

    client.post(PAGE, data={'relay_url': 'nope', 'relay_add': 'go', 'csrf_token': token})

    assert calls == []


@pytest.mark.parametrize('name, data', [('add_relay', {'relay_url': 'https://relay.example/inbox',
                                                       'relay_add': 'go'}),
                                        ('retry_relay', {'relay_retry': 'go'})])
def test_a_relay_error_is_flashed(admin, monkeypatch, name, data):
    client, token = admin
    relay = make_relay()

    def boom(*args):
        raise RelayError('cannot reach that relay')
    monkeypatch.setattr(views, name, boom)

    response = client.post(PAGE, data={**data, 'relay_id': str(relay.id), 'csrf_token': token})

    assert response.status_code == 302
    assert 'cannot reach that relay' in client.get(PAGE).get_data(as_text=True)


def test_retry_and_remove_act_on_the_row(admin, calls):
    client, token = admin
    relay = make_relay()

    client.post(PAGE, data={'relay_id': str(relay.id), 'relay_retry': 'go', 'csrf_token': token})
    client.post(PAGE, data={'relay_id': str(relay.id), 'relay_remove': 'go', 'csrf_token': token})

    assert calls == [('retry', relay.id), ('remove', relay.id)]


@pytest.mark.parametrize('after, message', [(RELAY_PENDING, 'Subscription request sent again.'),
                                            (RELAY_ACCEPTED, 'already lists this server as a follower')])
def test_retry_says_whether_it_followed_again_or_found_the_subscription(admin, monkeypatch, after, message):
    client, token = admin
    relay = make_relay(state=RELAY_PENDING)

    def retry(row):
        row.state = after
        db.session.commit()
    monkeypatch.setattr(views, 'retry_relay', retry)

    client.post(PAGE, data={'relay_id': str(relay.id), 'relay_retry': 'go', 'csrf_token': token})

    assert message in client.get(PAGE).get_data(as_text=True)


def test_an_unknown_id_is_a_404(admin, calls):
    client, token = admin

    response = client.post(PAGE, data={'relay_id': '999', 'relay_remove': 'go', 'csrf_token': token})

    assert response.status_code == 404 and calls == []


def test_a_non_numeric_id_is_a_404(admin, calls):
    client, token = admin

    response = client.post(PAGE, data={'relay_id': 'abc', 'relay_remove': 'go', 'csrf_token': token})

    assert response.status_code == 404 and calls == []


def test_an_invalid_url_flashes_its_error(admin, calls):
    client, token = admin

    client.post(PAGE, data={'relay_url': 'nope', 'relay_add': 'go', 'csrf_token': token})

    assert 'Invalid URL' in client.get(PAGE).get_data(as_text=True)


def test_an_out_of_range_retention_flashes_its_error(admin):
    client, token = admin

    client.post(PAGE, data={'relay_retention': '400', 'relay_settings_save': 'go', 'csrf_token': token})

    assert 'Number must be between 0 and 365' in client.get(PAGE).get_data(as_text=True)


def test_a_post_without_a_csrf_token_is_refused(admin, calls):
    client, _ = admin
    relay = make_relay()

    for data in ({'relay_url': 'https://relay.example/inbox', 'relay_add': 'go'},
                 {'relay_id': str(relay.id), 'relay_retry': 'go'},
                 {'relay_id': str(relay.id), 'relay_remove': 'go'}):
        assert client.post(PAGE, data=data).status_code == 400

    assert calls == []


def test_saving_the_retention_setting_stores_it(admin):
    client, token = admin

    client.post(PAGE, data={'relay_retention': '14', 'relay_settings_save': 'go', 'csrf_token': token})

    assert int(get_setting('relay_retention_days')) == 14
    assert 'value="14"' in client.get(PAGE).get_data(as_text=True)


def test_an_invalid_retention_is_not_stored(admin):
    client, token = admin

    client.post(PAGE, data={'relay_retention': '9999', 'relay_settings_save': 'go', 'csrf_token': token})

    assert get_setting('relay_retention_days') != 9999


def test_a_post_with_no_known_button_just_redirects(admin, calls):
    client, token = admin

    assert client.post(PAGE, data={'csrf_token': token}).status_code == 302 and calls == []


def test_the_nav_and_federation_page_link_here(admin):
    client, _ = admin

    assert f'href="{PAGE}"' in client.get('/admin/instances').get_data(as_text=True)
    assert f'href="{PAGE}"' in client.get('/admin/federation').get_data(as_text=True)


def test_the_federation_page_links_here_outside_the_bulk_import_sections(admin):
    client, _ = admin

    html = client.get('/admin/federation').get_data(as_text=True)

    # Relays are not a bulk import: the link sits in its own section, ahead of the import fieldsets
    assert html.index(f'href="{PAGE}"') < html.index('Bulk community import')


# --- CLI ---

@pytest.fixture
def run(app, monkeypatch):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none

    def invoke(*args):
        return app.test_cli_runner().invoke(args=['relays', *args])
    return invoke


def test_cli_add_prints_the_state(run, monkeypatch):
    monkeypatch.setattr(relay_cli.relay_subscribe, 'add_relay', lambda url: make_relay(url, RELAY_PENDING))

    result = run('add', 'https://relay.example/inbox')

    assert result.exit_code == 0 and f'https://relay.example/inbox: {STYLE_MASTODON}, pending' in result.output


@pytest.mark.parametrize('name, args', [('add_relay', ('add', 'https://r.example/inbox')),
                                        ('retry_relay', ('retry', 'https://relay.example/inbox'))])
def test_cli_relay_error_exits_1(run, monkeypatch, name, args):
    make_relay()

    def boom(*a):
        raise RelayError('nope, no relay there')
    monkeypatch.setattr(relay_cli.relay_subscribe, name, boom)

    result = run(*args)

    assert result.exit_code == 1 and 'nope, no relay there' in result.output


def test_cli_list_prints_one_line_per_relay(run):
    make_relay('https://a.example/inbox')
    make_relay('https://b.example/inbox')

    lines = run('list').output.strip().splitlines()

    assert len(lines) == 2 and lines[0].startswith('https://a.example/inbox\t')


def test_cli_retry_and_remove_by_url(run, monkeypatch):
    seen = []
    monkeypatch.setattr(relay_cli.relay_subscribe, 'retry_relay', lambda relay: seen.append(('retry', relay.url)))
    monkeypatch.setattr(relay_cli.relay_subscribe, 'remove_relay', lambda relay: seen.append(('remove', relay.url)))
    make_relay()

    retried = run('retry', 'https://relay.example/inbox')
    removed = run('remove', 'https://relay.example/inbox')

    assert seen == [('retry', 'https://relay.example/inbox'), ('remove', 'https://relay.example/inbox')]
    assert retried.exit_code == 0 and removed.output.strip() == 'removed'


@pytest.mark.parametrize('command', ['retry', 'remove'])
def test_cli_unknown_url_exits_1(run, command):
    result = run(command, 'https://nowhere.example/inbox')

    assert result.exit_code == 1 and 'no such relay' in result.output
