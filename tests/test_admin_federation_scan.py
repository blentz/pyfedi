"""`admin_federation_remote_scan` and `admin_federation_mastodon_scan`.

Sub-project 79, slice D. Both read a REMOTE server's API and then subscribe or
follow on the strength of what it said, so every row here is about what happens
when that server answers something the code did not expect -- an empty nodeinfo,
a page that never gets shorter, a software name nobody supports.

Four defects were found:

* `remote_scan` re-implemented "is this instance banned" as a membership test
  against the raw rows, exactly as `admin_federation_preload` did (D924's second
  copy, D931);
* a nodeinfo document with no 2.0 or 2.1 link left `remote_instanceinfo_url`
  unbound, so the admin got a 500 (D932);
* the three pagination loops end only when the REMOTE server returns a short
  page, so a server that always returns a full one never terminated (D933);
* `mastodon_scan` had no banned-instance check at all, so an admin could
  bulk-follow accounts from a defederated instance (D934).
"""
import json
from unittest.mock import Mock, patch

import pytest

from app import cache, db
from app.models import BannedInstances, Community, User
from tests.factories import grant_permission, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

NODEINFO = {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0',
                       'href': 'https://remote.example/nodeinfo/2.0'}]}


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1  # fact 347
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    return instance, ordinary


def _settings_admin(instance, name='settingsadmin'):
    """Holds ONLY 'change instance settings' and is not user 1 -- fact 348."""
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, 'change instance settings')
    return user


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def admin_client(app, db_session):
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client)


@pytest.fixture(autouse=True)
def no_memoized_answers():
    cache.clear()
    yield
    cache.clear()


def _scan(client, token, **overrides):
    data = {'remote_url': 'https://remote.example', 'communities_requested': '25',
            'minimum_posts': '100', 'minimum_active_users': '100',
            'remote_scan_submit': 'Scan', 'csrf_token': token}
    data.update(overrides)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        with patch('app.admin.routes.flash') as flashed:
            response = client.post('/admin/federation/remote_scan', data=data)
    return response, flashed, render


def _flashes(flashed):
    return [call.args[0] for call in flashed.call_args_list]


def _responses(*payloads):
    """`get_request` is called once per HTTP round trip and the route reads
    `.text`, so each payload becomes one response object in order."""
    return [Mock(text=json.dumps(payload)) for payload in payloads]


def _lemmy_community(actor_id, name='c', posts=1000, active=1000):
    return {'community': {'actor_id': actor_id, 'name': name},
            'counts': {'posts': posts, 'users_active_week': active}}


def _piefed_community(actor_id, name='c', posts=1000, active=1000):
    return {'community': {'actor_id': actor_id, 'name': name},
            'counts': {'post_count': posts, 'active_weekly': active}}


def _magazine(profile_id, name='m', entries=1000, subscribers=1000):
    return {'apProfileId': profile_id, 'name': name, 'entryCount': entries,
            'subscriptionsCount': subscribers}


def _software(name):
    return {'software': {'name': name}}


# --------------------------------------------------------------------------
# Authorization and input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['/admin/federation/remote_scan',
                                  '/admin/federation/mastodon_scan'])
def test_the_scan_pages_need_the_settings_permission(app, db_session, path):
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get(path)

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


@pytest.mark.parametrize('url', [
    'remote.example',                 # no scheme
    'http://remote.example',          # not https
    'https://remote.example/',        # trailing slash
    'https://remote.example/path',    # a path
    'https://',                       # nothing at all
])
def test_a_url_that_is_not_a_bare_https_host_is_refused(admin_client, url):
    """The route builds `f'{remote_url}/.well-known/nodeinfo'` by string
    concatenation, so anything but a bare `https://host` produces a URL that
    means something else. Refusing early is what keeps that from being an
    outbound request to a path the admin did not intend."""
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        response, flashed, _render = _scan(client, token, remote_url=url)

    assert response.status_code == 302
    assert get_request.call_args_list == []
    assert 'does not appear to be a valid url' in ' '.join(_flashes(flashed))


def test_a_banned_instance_is_not_scanned(admin_client):
    """D931's pin, inverted -- D924's second copy, in the other scan.

    The check read `server_domain in banned_urls`, a membership test against
    the raw `banned_instances` rows. A Mastodon-style wildcard ban is a
    PATTERN, so `'evil.example' in ['ev*l.example']` is False, and the test did
    no normalisation either. Scanning a defederated instance and subscribing to
    its communities is exactly what this guard exists to stop.
    """
    client, token = admin_client
    db.session.add(BannedInstances(domain='ev*l.example'))
    db.session.commit()
    cache.clear()

    with patch('app.admin.routes.get_request') as get_request:
        response, flashed, _render = _scan(client, token,
                                           remote_url='https://evil.example')

    assert response.status_code == 302
    assert get_request.call_args_list == [], 'a banned instance was contacted'
    assert 'is a banned instance' in ' '.join(_flashes(flashed))


def test_a_plainly_banned_instance_is_not_scanned(admin_client):
    """The control: a ban with no wildcard passed before the fix too, so
    without this row the one above could be satisfied by removing the check
    entirely."""
    client, token = admin_client
    db.session.add(BannedInstances(domain='evil.example'))
    db.session.commit()
    cache.clear()

    with patch('app.admin.routes.get_request') as get_request:
        _response, flashed, _render = _scan(client, token,
                                            remote_url='https://evil.example')

    assert get_request.call_args_list == []
    assert 'is a banned instance' in ' '.join(_flashes(flashed))


# --------------------------------------------------------------------------
# What the remote server says about itself
# --------------------------------------------------------------------------


def test_a_nodeinfo_without_a_supported_schema_link_is_not_a_500(admin_client):
    """D932's pin, inverted.

    `remote_instanceinfo_url` was only ever assigned inside the loop, so a
    nodeinfo document whose `links` contain no 2.0 or 2.1 entry -- an empty
    list is enough -- left the name unbound and the next line raised
    `UnboundLocalError`. The REMOTE server chooses that document, so this is a
    500 any server can hand the admin by answering something unexpected.
    """
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses({'links': []})
        response, flashed, _render = _scan(client, token)

    assert response.status_code == 302
    assert 'did not advertise a nodeinfo 2.0 or 2.1 document' in \
        ' '.join(_flashes(flashed))


def test_a_nodeinfo_advertising_schema_2_1_is_accepted(admin_client):
    """Both `schema2p0` and `schema2p1` are honoured, and only one row would
    otherwise cover either."""
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(
            {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.1',
                        'href': 'https://remote.example/nodeinfo/2.1'}]},
            _software('lemmy'),
            {'communities': []})
        response, flashed, _render = _scan(client, token, dry_run='y')

    assert response.status_code == 302
    assert get_request.call_args_list[1].args == ('https://remote.example/nodeinfo/2.1',)


@pytest.mark.parametrize('software', ['mastodon', 'peertube', 'wordpress'])
def test_an_unsupported_software_is_refused(admin_client, software):
    """The `else` arm. These three scans speak Lemmy-family APIs; carrying on
    would put a 404's HTML through `json.loads`."""
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software(software))
        response, flashed, _render = _scan(client, token)

    assert response.status_code == 302
    assert '/admin/federation' in response.headers['Location']
    assert 'does not appear to be a lemmy, mbin, or piefed instance' in \
        ' '.join(_flashes(flashed))


# --------------------------------------------------------------------------
# D933: the pagination loops
# --------------------------------------------------------------------------


@pytest.mark.parametrize('software, endpoint, page_key, full_page', [
    ('lemmy', '/api/v3/community/list', 'communities',
     [_lemmy_community(f'https://remote.example/c/{i}') for i in range(50)]),
    ('piefed', '/api/alpha/community/list', 'communities',
     [_piefed_community(f'https://remote.example/c/{i}') for i in range(50)]),
    ('mbin', '/api/magazines', 'items',
     [_magazine(f'https://remote.example/m/{i}') for i in range(50)]),
])
def test_a_server_that_never_returns_a_short_page_does_not_loop_forever(
        admin_client, software, endpoint, page_key, full_page):
    """D933's pin, inverted.

    Each loop ends only when a page comes back with fewer than 50 entries, and
    the REMOTE server decides how long every page is. A server that always
    answers with a full page kept the request going forever, holding a worker
    and growing the holding list without bound -- reachable by any instance an
    admin decides to scan, hostile or merely broken.

    The cap is asserted through the request count, which is what an unbounded
    loop would make infinite.
    """
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = ([Mock(text=json.dumps(NODEINFO)),
                                    Mock(text=json.dumps(_software(software)))]
                                   + [Mock(text=json.dumps({page_key: full_page}))] * 400)
        response, flashed, _render = _scan(client, token, dry_run='y')

    assert response.status_code == 302
    # Two nodeinfo round trips plus at most the page cap.
    paged = [call for call in get_request.call_args_list
             if endpoint in str(call.args[0])]
    assert len(paged) == 200
    assert [call.kwargs['params']['p' if software == 'mbin' else 'page']
            for call in paged][-1] == '200'


# --------------------------------------------------------------------------
# The three scan branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize('software, page_key, builder, dry_label', [
    ('lemmy', 'communities', _lemmy_community, 'Local Communities on the server'),
    ('piefed', 'communities', _piefed_community, 'Local Communities on the server'),
    ('mbin', 'items', _magazine, 'Local Magazines on the server'),
])
def test_a_dry_run_reports_the_counts_without_subscribing(
        admin_client, software, page_key, builder, dry_label):
    """`if dry_run:` in each of the three branches. A dry run must reach the
    remote server and then do nothing locally, so both halves are asserted."""
    client, token = admin_client
    page = [builder('https://remote.example/c/good'),
            builder('https://remote.example/c/quiet', posts=1)
            if software != 'mbin' else _magazine('https://remote.example/m/q',
                                                 entries=1)]

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software(software),
                                             {page_key: page})
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _response, flashed, _render = _scan(client, token, dry_run='y')

    message = ' '.join(_flashes(flashed))
    assert dry_label in message
    assert subscribe.delay.call_args_list == []


@pytest.mark.parametrize('software, page_key, builder', [
    ('lemmy', 'communities', _lemmy_community),
    ('piefed', 'communities', _piefed_community),
    ('mbin', 'items', _magazine),
])
def test_a_community_already_known_is_not_scanned_again(
        admin_client, software, page_key, builder):
    """The `already_known` filter in each branch, keyed on the actor id the
    remote server reports -- `actor_id` for the two Lemmy-family APIs and
    `apProfileId` for mbin, which is the only thing that differs between the
    three otherwise identical loops."""
    client, token = admin_client
    db.session.add(Community(name='known', title='known',
                             ap_public_url='https://remote.example/c/known'))
    db.session.commit()

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(
            NODEINFO, _software(software),
            {page_key: [builder('https://remote.example/c/known')]})
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _scan(client, token)

    assert subscribe.delay.call_args_list == []


@pytest.mark.parametrize('software, page_key, low, ok', [
    ('lemmy', 'communities',
     _lemmy_community('https://remote.example/c/low', posts=99),
     _lemmy_community('https://remote.example/c/ok', posts=100)),
    ('piefed', 'communities',
     _piefed_community('https://remote.example/c/low', posts=99),
     _piefed_community('https://remote.example/c/ok', posts=100)),
    ('mbin', 'items',
     _magazine('https://remote.example/m/low', entries=99),
     _magazine('https://remote.example/m/ok', entries=100)),
])
def test_the_minimum_post_count_is_strict(admin_client, software, page_key,
                                          low, ok):
    """`< min_posts`, not `<=`. The admin types the minimum, so the boundary is
    the difference between honouring the number they entered and excluding it."""
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software(software),
                                             {page_key: [low, ok]})
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = Mock(ap_id='found')
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token, minimum_posts='100')

    assert subscribe.delay.call_count == 1


@pytest.mark.parametrize('software, page_key, low, ok', [
    ('lemmy', 'communities',
     _lemmy_community('https://remote.example/c/low', active=99),
     _lemmy_community('https://remote.example/c/ok', active=100)),
    ('piefed', 'communities',
     _piefed_community('https://remote.example/c/low', active=99),
     _piefed_community('https://remote.example/c/ok', active=100)),
    ('mbin', 'items',
     _magazine('https://remote.example/m/low', subscribers=99),
     _magazine('https://remote.example/m/ok', subscribers=100)),
])
def test_the_minimum_user_count_is_strict(admin_client, software, page_key,
                                          low, ok):
    """`< min_users`. mbin reports no weekly actives, so its branch uses the
    subscriber count for the same field -- asserted here so the substitution is
    recorded rather than assumed."""
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software(software),
                                             {page_key: [low, ok]})
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = Mock(ap_id='found')
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token, minimum_active_users='100')

    assert subscribe.delay.call_count == 1


@pytest.mark.parametrize('software, page_key, builder', [
    ('lemmy', 'communities', _lemmy_community),
    ('piefed', 'communities', _piefed_community),
    ('mbin', 'items', _magazine),
])
def test_a_community_whose_name_is_a_bad_word_is_excluded(
        admin_client, software, page_key, builder):
    client, token = admin_client

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(
            NODEINFO, _software(software),
            {page_key: [builder('https://remote.example/c/bad', name='bad')]})
        with patch('app.admin.routes.is_bad_name', return_value=True):
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token)

    assert subscribe.delay.call_args_list == []


@pytest.mark.parametrize('software, page_key, builder', [
    ('lemmy', 'communities', _lemmy_community),
    ('piefed', 'communities', _piefed_community),
    ('mbin', 'items', _magazine),
])
def test_asking_for_fewer_than_are_available_takes_that_many(
        admin_client, software, page_key, builder):
    """`if communities_requested > len(candidate_communities)` -- both arms, in
    each branch. Without the clamp the `range` loop would raise IndexError; with
    it, asking for fewer than exist must still take only that many."""
    client, token = admin_client
    page = [builder(f'https://remote.example/c/{i}') for i in range(5)]

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software(software),
                                             {page_key: page})
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = Mock(ap_id='found')
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token, communities_requested='2')

    assert subscribe.delay.call_count == 2


def test_asking_for_more_than_are_available_takes_all_of_them(admin_client):
    client, token = admin_client
    page = [_lemmy_community(f'https://remote.example/c/{i}') for i in range(3)]

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software('lemmy'),
                                             {'communities': page})
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = Mock(ap_id='found')
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token, communities_requested='500')

    assert subscribe.delay.call_count == 3


def test_a_community_that_cannot_be_found_is_skipped(admin_client):
    """`if not new_community: continue`. search_for_community reaches the
    remote instance, so a candidate that passed every filter can still be
    unreachable."""
    client, token = admin_client
    page = [_lemmy_community('https://remote.example/c/gone'),
            _lemmy_community('https://remote.example/c/here')]

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software('lemmy'),
                                             {'communities': page})
        with patch('app.admin.routes.search_for_community') as search:
            search.side_effect = [None, Mock(ap_id='here')]
            with patch('app.admin.routes.do_subscribe') as subscribe:
                _scan(client, token)

    assert subscribe.delay.call_count == 1
    assert subscribe.delay.call_args.args == ('here', 1)
    assert subscribe.delay.call_args.kwargs == {'admin_preload': True}


def test_the_scan_runs_inline_and_reports_results_in_debug(app, admin_client):
    """`if current_app.debug:` -- debug subscribes synchronously and flashes
    what each call returned, which is the only way an operator sees why one
    failed."""
    client, token = admin_client
    page = [_lemmy_community('https://remote.example/c/x')]

    with patch('app.admin.routes.get_request') as get_request:
        get_request.side_effect = _responses(NODEINFO, _software('lemmy'),
                                             {'communities': page})
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = Mock(ap_id='found')
            with patch('app.admin.routes.do_subscribe',
                       return_value='subscribed ok') as subscribe:
                with patch.dict(app.config, {'DEBUG': True}):
                    _response, flashed, _render = _scan(client, token)

    assert subscribe.call_args.args == ('found', 1)
    assert any('subscribed ok' in message for message in _flashes(flashed))


def test_the_remote_scan_page_renders_without_a_submission(admin_client):
    """The `return render_template(...)` past the submit branch -- every other
    row here ends in a redirect."""
    client, token = admin_client

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/federation/remote_scan')

    assert response.status_code == 200
    assert render.call_args.args == ('admin/federation_remote_scan.html',)


# --------------------------------------------------------------------------
# admin_federation_mastodon_scan
# --------------------------------------------------------------------------


def _mastodon(client, token, **overrides):
    data = {'mastodon_url': 'https://mastodon.example', 'accounts_requested': '25',
            'minimum_statuses': '50', 'minimum_followers': '10',
            'mastodon_scan_submit': 'Scan', 'csrf_token': token}
    data.update(overrides)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        with patch('app.admin.routes.flash') as flashed:
            response = client.post('/admin/federation/mastodon_scan', data=data)
    return response, flashed, render


def test_a_banned_mastodon_instance_is_not_scanned(admin_client):
    """D934's pin, inverted.

    The two community scans on this page refuse a banned instance and this one
    did not check at all. `bulk_follow` resolves each handle through
    `search_for_user`, which fetches the actor and creates local rows for it, so
    following accounts on a defederated instance IS federating with it.
    """
    client, token = admin_client
    db.session.add(BannedInstances(domain='mastodon.example'))
    db.session.commit()
    cache.clear()

    with patch('app.admin.routes.remote_instance_software', return_value='mastodon'):
        with patch('app.admin.routes.fetch_mastodon_directory') as directory:
            with patch('app.admin.routes.bulk_follow') as follow:
                response, flashed, _render = _mastodon(client, token)

    assert response.status_code == 302
    assert directory.call_args_list == [], 'a banned instance was contacted'
    assert follow.delay.call_args_list == []
    assert 'is a banned instance' in ' '.join(_flashes(flashed))


def test_a_server_whose_nodeinfo_cannot_be_read_is_refused(admin_client):
    """`except Exception:` around `remote_instance_software`. The remote server
    is not obliged to answer, and the admin gets a message rather than a 500."""
    client, token = admin_client

    with patch('app.admin.routes.remote_instance_software',
               side_effect=OSError('connection refused')):
        response, flashed, _render = _mastodon(client, token)

    assert response.status_code == 302
    assert 'Could not read nodeinfo' in ' '.join(_flashes(flashed))


def test_a_server_that_is_not_mastodon_is_refused(admin_client):
    """`/api/v1/directory` only exists on Mastodon and its close forks, so the
    check turns a later 404 into a message that names the software found."""
    client, token = admin_client

    with patch('app.admin.routes.remote_instance_software', return_value='lemmy'):
        with patch('app.admin.routes.fetch_mastodon_directory') as directory:
            response, flashed, _render = _mastodon(client, token)

    assert response.status_code == 302
    assert directory.call_args_list == []
    assert 'reports its software as' in ' '.join(_flashes(flashed))


def test_a_mastodon_dry_run_reports_the_counts_without_following(admin_client):
    client, token = admin_client
    stats = {'seen': 10, 'bots': 2, 'below_minimum_statuses': 3,
             'below_minimum_followers': 1, 'candidates': 4}

    with patch('app.admin.routes.remote_instance_software', return_value='mastodon'):
        with patch('app.admin.routes.fetch_mastodon_directory', return_value=[]):
            with patch('app.admin.routes.directory_candidates',
                       return_value=(['a@mastodon.example'], stats)):
                with patch('app.admin.routes.bulk_follow') as follow:
                    _response, flashed, _render = _mastodon(client, token,
                                                            mastodon_dry_run='y')

    assert 'Dry run for' in ' '.join(_flashes(flashed))
    assert follow.delay.call_args_list == []


def test_a_mastodon_scan_matching_nothing_says_so(admin_client):
    """`if not handles:` -- an empty follow list must not be queued as one."""
    client, token = admin_client
    stats = {'seen': 10, 'bots': 10, 'below_minimum_statuses': 0,
             'below_minimum_followers': 0, 'candidates': 0}

    with patch('app.admin.routes.remote_instance_software', return_value='mastodon'):
        with patch('app.admin.routes.fetch_mastodon_directory', return_value=[]):
            with patch('app.admin.routes.directory_candidates',
                       return_value=([], stats)):
                with patch('app.admin.routes.bulk_follow') as follow:
                    _response, flashed, _render = _mastodon(client, token)

    assert 'matched those filters' in ' '.join(_flashes(flashed))
    assert follow.delay.call_args_list == []


def test_a_mastodon_scan_queues_the_follows_as_user_one(admin_client):
    """The filters typed into the form are passed through to
    `directory_candidates`, and the follows are queued as user 1 to match the
    two community scans -- both asserted, because a scan that follows as the
    wrong user produces subscriptions the admin cannot undo."""
    client, token = admin_client
    handles = ['a@mastodon.example', 'b@mastodon.example']
    stats = {'seen': 2, 'bots': 0, 'below_minimum_statuses': 0,
             'below_minimum_followers': 0, 'candidates': 2}

    with patch('app.admin.routes.remote_instance_software', return_value='mastodon'):
        with patch('app.admin.routes.fetch_mastodon_directory',
                   return_value=['account']) as directory:
            with patch('app.admin.routes.directory_candidates',
                       return_value=(handles, stats)) as candidates:
                with patch('app.admin.routes.bulk_follow') as follow:
                    _response, flashed, _render = _mastodon(
                        client, token, minimum_statuses='7',
                        minimum_followers='3', accounts_requested='9',
                        exclude_bots='y')

    assert directory.call_args.args == ('https://mastodon.example',)
    assert candidates.call_args.args == (['account'], 'mastodon.example')
    assert candidates.call_args.kwargs == {'minimum_statuses': 7,
                                           'minimum_followers': 3,
                                           'exclude_bots': True, 'limit': 9}
    assert follow.delay.call_args.args == (1, handles)
    assert 'Following 2 accounts' in ' '.join(_flashes(flashed))


def test_a_mastodon_scan_runs_inline_in_debug(app, admin_client):
    client, token = admin_client
    stats = {'seen': 1, 'bots': 0, 'below_minimum_statuses': 0,
             'below_minimum_followers': 0, 'candidates': 1}

    with patch('app.admin.routes.remote_instance_software', return_value='mastodon'):
        with patch('app.admin.routes.fetch_mastodon_directory', return_value=[]):
            with patch('app.admin.routes.directory_candidates',
                       return_value=(['a@mastodon.example'], stats)):
                with patch('app.admin.routes.bulk_follow') as follow:
                    with patch.dict(app.config, {'DEBUG': True}):
                        _mastodon(client, token)

    assert follow.call_args.args == (1, ['a@mastodon.example'])
    assert follow.delay.call_args_list == []


def test_a_trailing_slash_on_the_mastodon_url_is_stripped(admin_client):
    """`.strip().rstrip('/')`. The URL is concatenated with API paths, so a
    trailing slash would produce `https://host//api/v1/directory`."""
    client, token = admin_client

    with patch('app.admin.routes.remote_instance_software',
               return_value='mastodon') as software:
        with patch('app.admin.routes.fetch_mastodon_directory',
                   return_value=[]) as directory:
            with patch('app.admin.routes.directory_candidates',
                       return_value=([], {})):
                _mastodon(client, token,
                          mastodon_url='  https://mastodon.example/  ')

    assert software.call_args.args == ('https://mastodon.example',)
    assert directory.call_args.args == ('https://mastodon.example',)


def test_the_mastodon_scan_page_renders_without_a_submission(admin_client):
    client, token = admin_client

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/federation/mastodon_scan')

    assert response.status_code == 200
    assert render.call_args.args == ('admin/federation_mastodon_scan.html',)
