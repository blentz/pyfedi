"""Interop D24: the daily refresh cleans what the directories send, filters it, caps it, upserts it on
actor_url, and forgets what nobody has listed for 30 days."""
import logging
from datetime import timedelta

import pytest

from app import db
from app.discovery import filters, refresh, sources
from app.discovery.filters import request_host
from app.discovery.refresh import clean_entries, refresh_discovery
from app.models import DiscoveryEntry, Domain, utcnow
from tests.discovery_fixtures import fresh_cache, nobody_excluded  # noqa: F401
from tests.factories import make_banned_instance

NOW = utcnow()


pytestmark = pytest.mark.usefixtures('fresh_cache')


@pytest.fixture
def only(monkeypatch):
    """Every fetcher returns nothing unless the test installs one; no isolation list is fetched."""
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset())
    for source in list(refresh.FETCHERS):
        monkeypatch.setitem(refresh.FETCHERS, source, lambda exclude: [])

    def install(source, result):
        monkeypatch.setitem(refresh.FETCHERS, source, result if callable(result) else (lambda exclude: result))
    return install


def entry(name='Linux Videos', host='tube.example', slug=None, **changes):
    values = {'kind': 'community', 'platform': 'peertube',
              'actor_url': f'https://{host}/video-channels/{slug or name.lower().replace(" ", "_")}',
              'name': name, 'host': host, 'avatar': None, 'followers': 1, 'nsfw': False, 'source': 'sepiasearch'}
    values.update(changes)
    return values


def test_an_upsert_is_idempotent_and_keeps_first_seen(app, db_session, only):
    only('sepiasearch', [entry(followers=1)])
    refresh_discovery(now=NOW - timedelta(days=2))
    only('sepiasearch', [entry(followers=7)])

    result = refresh_discovery(now=NOW)

    row = DiscoveryEntry.query.one()
    assert result['sepiasearch'] == 1
    assert row.followers == 7
    assert row.first_seen == NOW - timedelta(days=2)
    assert row.last_seen == NOW


def test_entries_not_seen_for_thirty_days_expire(app, db_session, only):
    db.session.add_all([
        DiscoveryEntry(kind='community', platform='peertube', actor_url='https://old.example/video-channels/a',
                       name='Old', host='old.example', source='sepiasearch',
                       first_seen=NOW - timedelta(days=40), last_seen=NOW - timedelta(days=31)),
        DiscoveryEntry(kind='community', platform='peertube', actor_url='https://new.example/video-channels/b',
                       name='Recent', host='new.example', source='sepiasearch',
                       first_seen=NOW - timedelta(days=40), last_seen=NOW - timedelta(days=29))])
    db.session.commit()

    result = refresh_discovery(now=NOW)

    assert result['expired'] == 1
    assert [row.name for row in DiscoveryEntry.query.all()] == ['Recent']


def test_a_failing_source_keeps_its_entries_and_the_others_still_run(app, db_session, only):
    db.session.add(DiscoveryEntry(kind='community', platform='peertube', actor_url='https://t.example/video-channels/k',
                                  name='Kept', host='t.example', source='sepiasearch',
                                  first_seen=NOW - timedelta(days=10), last_seen=NOW - timedelta(days=10)))
    db.session.commit()

    def broken(exclude):
        raise sources.DiscoverySourceError('down')
    only('sepiasearch', broken)
    only('joinmastodon', [entry(name='Ann', host='m.example', kind='person', platform='mastodon',
                                actor_url='https://m.example/users/ann', source='joinmastodon')])

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 'failed'
    assert result['joinmastodon'] == 1
    kept = DiscoveryEntry.query.filter_by(name='Kept').one()
    assert kept.last_seen == NOW - timedelta(days=10)


def test_names_and_urls_are_cleaned(app, db_session):
    rows = [entry(name='  <b>Linux</b>\x00 Videos  ', slug='linux', avatar='https://tube.example/a.png'),
            entry(name='Plain http', host='h.example', actor_url='http://h.example/video-channels/x'),
            entry(name='Script avatar', host='s.example', avatar='javascript:alert(1)'),
            entry(name='Wrong host', host='w.example', actor_url='https://elsewhere.example/video-channels/x'),
            entry(name='   ', host='e.example', slug='blank')]

    cleaned = clean_entries(rows, nobody_excluded)

    assert [(e['name'], e['avatar']) for e in cleaned] == [('Linux Videos', 'https://tube.example/a.png'),
                                                           ('Script avatar', None)]


def test_banned_blocked_isolated_and_badly_named_entries_are_dropped(app, db_session, only, monkeypatch):
    make_banned_instance('banned.example')
    db.session.add(Domain(name='blockeddomain.example', banned=True))
    db.session.commit()
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset({'isolated.example'}))
    only('sepiasearch', [entry(name='Fine'), entry(name='Banned', host='banned.example'),
                         entry(name='Blocked', host='blockeddomain.example'),
                         entry(name='Isolated', host='isolated.example'), entry(name='shitposting central')])

    refresh_discovery(now=NOW)

    assert [row.name for row in DiscoveryEntry.query.all()] == ['Fine']


def test_nsfw_is_tagged_from_the_source_or_the_name(app, db_session):
    cleaned = clean_entries([entry(name='Art'), entry(name='Art NSFW', slug='a2'), entry(name='Flagged', nsfw=True)],
                            nobody_excluded)

    assert [(e['name'], e['nsfw']) for e in cleaned] == [('Art', False), ('Art NSFW', True), ('Flagged', True)]


def test_entries_are_capped_per_host_and_per_source(app, db_session, monkeypatch):
    many = [entry(name=f'Channel {i}', slug=f'c{i}') for i in range(25)]
    assert len(clean_entries(many, nobody_excluded)) == refresh.MAX_PER_HOST == 20

    monkeypatch.setattr(refresh, 'MAX_PER_SOURCE', 3)
    spread = [entry(name=f'Channel {i}', host=f'h{i}.example') for i in range(10)]
    assert len(clean_entries(spread, nobody_excluded)) == 3


def test_a_duplicate_actor_in_one_run_is_stored_once(app, db_session, only):
    """Review focus 2: ON CONFLICT DO UPDATE refuses to touch one row twice in one statement."""
    only('sepiasearch', [entry(followers=1), entry(followers=2)])

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 1
    assert DiscoveryEntry.query.count() == 1


def test_the_isolation_list_is_read_into_lower_case_hosts(app, monkeypatch):
    monkeypatch.setattr(filters, 'retrieve_peertube_block_list', lambda: 'a.example\nB.Example\n\n')

    assert filters.peertube_isolated_hosts() == frozenset({'a.example', 'b.example'})


def test_an_unreachable_isolation_list_is_empty(app, monkeypatch):
    monkeypatch.setattr(filters, 'retrieve_peertube_block_list', lambda: None)

    assert filters.peertube_isolated_hosts() == frozenset()


def test_allowlist_mode_excludes_hosts_not_on_the_list(app, db_session):
    from app.models import AllowedInstances
    from app.utils import set_setting
    set_setting('use_allowlist', True)
    db.session.add(AllowedInstances(domain='allowed.example'))
    db.session.commit()

    assert filters.host_is_excluded('allowed.example', frozenset()) is False
    assert filters.host_is_excluded('other.example', frozenset()) is True


def test_an_overlong_actor_url_is_skipped_not_fatal(app, db_session, only):
    long_slug = 'x' * filters.URL_LIMIT
    only('sepiasearch', [entry(name='Too long', slug=long_slug), entry(name='Fine')])

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 1
    assert [row.name for row in DiscoveryEntry.query.all()] == ['Fine']


def test_an_overlong_actor_url_is_logged(app, db_session, caplog):
    long_url = f'https://tube.example/video-channels/{"x" * filters.URL_LIMIT}'
    caplog.set_level(logging.INFO)

    cleaned = clean_entries([entry(name='Too long', actor_url=long_url), entry(name='Fine')], nobody_excluded)

    assert [e['name'] for e in cleaned] == ['Fine']
    assert 'over-long actor_url' in caplog.text
    assert long_url not in caplog.text   # the length and host are enough; the url itself may be huge


def test_a_malformed_url_is_dropped_not_fatal(app, db_session):
    rows = [entry(name='Bad actor', actor_url='https://[bad/a'),
            entry(name='Bad avatar', host='b.example', avatar='https://[bad/a')]

    cleaned = clean_entries(rows, nobody_excluded)

    assert [(e['name'], e['avatar']) for e in cleaned] == [('Bad avatar', None)]


def test_a_source_that_cannot_be_stored_leaves_the_others_and_expiry_running(app, db_session, only, monkeypatch):
    db.session.add(DiscoveryEntry(kind='community', platform='peertube', actor_url='https://old.example/video-channels/a',
                                  name='Old', host='old.example', source='sepiasearch',
                                  first_seen=NOW - timedelta(days=40), last_seen=NOW - timedelta(days=31)))
    db.session.commit()
    only('sepiasearch', [entry(name='Fine')])
    only('joinmastodon', [entry(name='Ann', host='m.example', kind='person', platform='mastodon',
                                actor_url='https://m.example/users/ann', source='joinmastodon')])
    real = refresh.upsert_entries

    def upsert(entries, now):
        if entries and entries[0]['source'] == 'sepiasearch':
            raise RuntimeError('db down')
        return real(entries, now)
    monkeypatch.setattr(refresh, 'upsert_entries', upsert)

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 'failed'
    assert result['joinmastodon'] == 1
    assert result['expired'] == 1


def test_an_overlong_host_is_skipped(app, db_session):
    host = 'a' * 250 + '.example'

    assert clean_entries([entry(name='Long', host=host)], nobody_excluded) == []


def test_an_isolation_list_that_raises_is_empty(app, monkeypatch):
    def broken():
        raise ValueError('bad json')
    monkeypatch.setattr(filters, 'retrieve_peertube_block_list', broken)

    assert filters.peertube_isolated_hosts() == frozenset()


@pytest.mark.parametrize('url, host', [('https://Ok.Example/x', 'ok.example'), ('https://ok.example./x', 'ok.example'),
                                       ('https://bücher.example/x', 'xn--bcher-kva.example'),
                                       ('https://ok.example:8443/x', 'ok.example'),
                                       ('https://[@ok.example/x', None), ('https://u@ok.example/x', None),
                                       ('https://a.example\\@ok.example/x', None), ('not a url', None), (None, None)])
def test_request_host_is_the_host_httpx_connects_to_or_a_refusal(app, url, host):
    assert request_host(url) == host


@pytest.mark.parametrize('url', ['https://bücher.example/feed', 'https://xn--bcher-kva.example/feed',
                                 'https://BÜCHER.example./feed', 'https://XN--BCHER-KVA.example/feed'])
def test_request_host_accepts_an_internationalised_host_in_either_form(app, url):
    assert request_host(url) == 'xn--bcher-kva.example'


@pytest.mark.parametrize('url', ['https://[@ok.example/x', 'https://u@ok.example/x', 'https://a.example\\@ok.example/x',
                                 'https://u@bücher.example/feed', 'https://xn--bcher-kva.example@ok.example/feed',
                                 'https://u@xn--bcher-kva.example/feed', 'not a url', None])
def test_request_host_still_refuses_userinfo_and_parser_disagreement(app, url):
    assert request_host(url) is None


@pytest.mark.parametrize('url', ['https://evil。example/', 'https://evil．example/', 'https://evil｡example/', 'https://ｅｖｉｌ.example/',
                                 'https://ﬁle.example/'])
def test_request_host_refuses_a_host_that_only_httpx_maps_to_another(app, url):
    """httpx's IDNA encoder reads an ideographic, fullwidth or halfwidth full stop as a label separator
    (evil.example), which the stdlib -- and anything judging its host -- does not; a fullwidth letter or a ligature
    fails strict IDNA 2008 encoding."""
    assert request_host(url) is None
