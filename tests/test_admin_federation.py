"""The federation allow/block surface: `admin_federation`,
`admin_federation_preload`, `admin_federation_ban_lists` and
`import_bans_task`.

Sub-project 79, slice C. This is the instance's core moderation control: what
`admin_federation` writes is what `instance_banned()` and `instance_allowed()`
read on every inbound activity and every outbound delivery.

Three defects were found here, and one of them had made a whole feature silent:

* `import_bans_task` tested `isinstance(instance_allowed, list)` -- the
  FUNCTION imported from `app.utils`, not its local `instances_allowed` -- so
  importing a ban list in allowlist mode imported nothing, with no error
  (D922);
* a missing key in the uploaded JSON raised mid-import, after earlier sections
  had already committed (D923);
* `admin_federation_preload` re-implemented "is this instance banned" as a
  membership test against the raw rows, which misses every wildcard ban and
  does no normalisation, so preload could subscribe to communities on a
  defederated instance (D924).

The two ways past a ban that this slice also fixed -- unescaped wildcard
patterns and the trailing-dot bypass -- are pinned in
`tests/test_instance_ban_matching.py`, because they live in `app/utils.py`.
"""
import io
import json
import os
from unittest.mock import Mock, patch

import pytest

from app import cache, db
from app.models import (AllowedInstances, BannedInstances,
                        DefederationSubscription, Domain, Site, Tag, User)
from tests.factories import grant_permission, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

MEDIA = 'app/static/media'


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
    """instance_banned and instance_allowed are memoized for 150 seconds, and
    these rows write exactly what those two read."""
    cache.clear()
    yield
    cache.clear()


def _federation_payload(token, **overrides):
    data = {'federation_mode': 'blocklist', 'allowlist': '', 'allowlist_mode': '0',
            'blocklist': '', 'defederation_subscription': '',
            'blocked_phrases': '', 'blocked_actors': '', 'blocked_bio': '',
            'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


def _post(client, token, path='/admin/federation', **overrides):
    """Fact 363: the real template is not what is under test here."""
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.post(path, data=_federation_payload(token, **overrides))
    return response, render


# --------------------------------------------------------------------------
# admin_federation: authorization, and what it writes
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['/admin/federation', '/admin/federation/preload',
                                  '/admin/federation/ban_lists'])
def test_the_federation_pages_need_the_settings_permission(app, db_session, path):
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get(path)

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


def test_the_blocklist_is_stored_lowercased_and_stripped(admin_client):
    """Each line becomes one BannedInstances row. The `.strip().lower()` is
    what makes the rows match what `inbox_domain` normalises a peer's domain
    to, so it is asserted rather than assumed."""
    client, token = admin_client

    _post(client, token, blocklist='Evil.COM\n  spam.example  \n\n\nthird.example\n')

    domains = sorted(row.domain for row in BannedInstances.query.all())
    assert domains == ['evil.com', 'spam.example', 'third.example']


def test_a_blank_line_in_the_blocklist_does_not_become_a_row(admin_client):
    """`if banned.strip():` -- an empty domain in banned_instances would make
    `instance_banned('')` ambiguous, and the column is what every federation
    check reads."""
    client, token = admin_client

    _post(client, token, blocklist='\n\n   \n\n')

    assert BannedInstances.query.count() == 0


def test_saving_the_form_replaces_the_previous_blocklist(admin_client):
    """`DELETE FROM banned_instances WHERE subscription_id is null` -- the box
    is the whole list, not an addition to it. A domain removed from the textarea
    must stop being banned."""
    client, token = admin_client
    db.session.add(BannedInstances(domain='was.banned'))
    db.session.commit()

    _post(client, token, blocklist='now.banned')

    domains = [row.domain for row in BannedInstances.query.all()]
    assert domains == ['now.banned']


def test_a_subscription_sourced_ban_is_replaced_separately(admin_client):
    """Bans with a `subscription_id` are not in the textarea, so the
    `is null` half of the delete is what keeps the admin's own list from
    wiping them -- and the second delete plus re-download is what refreshes
    them. Without the `is null`, saving the form would silently drop every
    subscribed ban until the next download."""
    client, token = admin_client
    subscription = DefederationSubscription(domain='source.example')
    db.session.add(subscription)
    db.session.commit()
    db.session.add(BannedInstances(domain='from.subscription',
                                   subscription_id=subscription.id))
    db.session.commit()

    with patch('app.admin.routes.download_defeds') as download:
        _post(client, token, blocklist='typed.by.hand',
              defederation_subscription='source.example')

    # The subscribed ban is deleted and re-fetched, not preserved in place.
    assert [row.domain for row in BannedInstances.query.all()] == ['typed.by.hand']
    assert download.call_count == 1
    subscription = DefederationSubscription.query.one()
    assert download.call_args.args == (subscription.id, 'source.example')


def test_the_allowlist_is_stored_the_same_way(admin_client):
    client, token = admin_client

    _post(client, token, federation_mode='allowlist',
          allowlist='Good.COM\n  fine.example  \n\n')

    assert sorted(row.domain for row in AllowedInstances.query.all()) == [
        'fine.example', 'good.com']


def test_the_federation_mode_is_stored_as_a_boolean_setting(admin_client):
    """`use_allowlist` is what `community/util.py:38` and the export branch of
    admin_federation_ban_lists read, so both values are asserted."""
    from app.utils import get_setting

    client, token = admin_client

    _post(client, token, federation_mode='allowlist')
    assert get_setting('use_allowlist', None) is True

    _post(client, token, federation_mode='blocklist')
    assert get_setting('use_allowlist', None) is False


def test_the_remaining_federation_settings_are_stored(admin_client):
    from app.utils import get_setting

    client, token = admin_client

    _post(client, token, allowlist_mode='2', blocked_phrases='one\ntwo',
          blocked_actors='spammer', blocked_bio='crypto',
          auto_add_remote_communities='y')

    db.session.expire_all()
    site = db.session.get(Site, 1)
    assert site.allowlist_mode == 2
    assert site.blocked_phrases == 'one\ntwo'
    assert get_setting('actor_blocked_words', '') == 'spammer'
    assert get_setting('actor_bio_blocked_words', '') == 'crypto'
    assert get_setting('auto_add_remote_communities', None) is True


def test_saving_the_form_invalidates_the_phrase_caches(admin_client):
    """`blocked_phrases` and `actor_blocked_words` are memoized and are read on
    every inbound post, so a phrase added here must take effect now rather than
    when the cache expires."""
    from app.utils import blocked_phrases, get_setting

    client, token = admin_client

    with patch('app.admin.routes.cache.delete_memoized') as delete_memoized:
        _post(client, token, blocked_phrases='a banned phrase')

    invalidated = [call.args for call in delete_memoized.call_args_list]
    assert (blocked_phrases,) in invalidated
    assert (get_setting, 'actor_blocked_words') in invalidated


def test_the_form_is_prefilled_from_what_is_stored(admin_client):
    """`elif request.method == 'GET':`. The blocklist box must show only the
    admin's own bans -- a subscribed ban appearing there would be re-saved as a
    hand-typed one and lose its subscription_id."""
    from app.utils import set_setting

    client, token = admin_client
    subscription = DefederationSubscription(domain='source.example')
    db.session.add_all([subscription, BannedInstances(domain='typed.example'),
                        AllowedInstances(domain='allowed.example')])
    db.session.commit()
    db.session.add(BannedInstances(domain='subscribed.example',
                                   subscription_id=subscription.id))
    site = db.session.get(Site, 1)
    site.blocked_phrases = 'stored phrase'
    site.allowlist_mode = 1
    db.session.commit()
    set_setting('use_allowlist', True)
    set_setting('actor_blocked_words', 'stored actor')
    set_setting('actor_bio_blocked_words', 'stored bio')

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/federation')

    form = render.call_args.kwargs['form']
    assert form.federation_mode.data == 'allowlist'
    assert form.blocklist.data == 'typed.example'
    assert form.allowlist.data == 'allowed.example'
    assert form.allowlist_mode.data == '1'
    assert form.defederation_subscription.data == 'source.example'
    assert form.blocked_phrases.data == 'stored phrase'
    assert form.blocked_actors.data == 'stored actor'
    assert form.blocked_bio.data == 'stored bio'


def test_the_blocklist_reaches_instance_banned(admin_client):
    """The end-to-end assertion, and the only one that says the page does its
    job: what is typed into the box is what the federation gate reads."""
    from app.utils import instance_banned

    client, token = admin_client

    _post(client, token, blocklist='Evil.COM\nev*l.example')
    cache.clear()

    assert instance_banned('evil.com') is True
    assert instance_banned('https://EVIL.COM./inbox') is True
    assert instance_banned('evXl.example') is True
    assert instance_banned('elsewhere.example') is False


# --------------------------------------------------------------------------
# admin_federation_preload
# --------------------------------------------------------------------------


def _community(url, baseurl, name='general', nsfw=False, posts=1000,
               active=1000):
    return {'url': url, 'baseurl': baseurl, 'name': name, 'nsfw': nsfw,
            'counts': {'posts': posts, 'users_active_week': active}}


@pytest.fixture
def lemmyverse():
    """`admin_federation_preload` fetches a large JSON from data.lemmyverse.net.
    The fixture patches the fetch rather than the parsing, so the route's own
    filtering is what runs."""
    with patch('app.admin.routes.get_request') as get_request:
        yield get_request


def _preload(client, token, communities, **form):
    data = {'communities_num': '25', 'pre_load_submit': 'Add Communities',
            'csrf_token': token}
    data.update(form)
    with patch('app.admin.routes.render_template', return_value='rendered'):
        with patch('app.admin.routes.flash') as flashed:
            response = client.post('/admin/federation/preload', data=data)
    return response, flashed


def _flashes(flashed):
    return [call.args[0] for call in flashed.call_args_list]


@pytest.mark.parametrize('community, reason', [
    (_community('https://a.example/c/known', 'a.example'), 'already known'),
    (_community('https://b.example/c/x', 'b.example', nsfw=True), 'nsfw'),
    (_community('https://c.example/c/x', 'c.example', posts=99), 'too few posts'),
    (_community('https://d.example/c/x', 'd.example', active=499), 'too few active'),
])
def test_each_preload_filter_excludes_its_own_case(admin_client, lemmyverse,
                                                   community, reason):
    """The five `continue`s, one row each. They are asserted through the count
    in the flash message rather than through subscriptions, because a filtered
    community produces no observable action at all."""
    from app.models import Community

    client, token = admin_client
    if reason == 'already known':
        db.session.add(Community(name='known', ap_public_url=community['url'],
                                 title='known'))
        db.session.commit()
    lemmyverse.return_value.json.return_value = [community]

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value = None
        _response, flashed = _preload(client, token, [community])

    assert '1 out of 1 communities were excluded using current filters' in \
        ' '.join(_flashes(flashed))


@pytest.mark.parametrize('posts, active, included', [
    (100, 1000, True),   # exactly 100 posts is enough
    (99, 1000, False),
    (1000, 500, True),   # exactly 500 weekly actives is enough
    (1000, 499, False),
])
def test_the_preload_thresholds_are_strict(admin_client, lemmyverse, posts,
                                           active, included):
    """`< 100` and `< 500`, not `<=`. The boundary decides whether a community
    sitting exactly on the published threshold is preloaded, and nothing else
    in the route distinguishes the two operators."""
    client, token = admin_client
    community = _community('https://edge.example/c/x', 'edge.example',
                           posts=posts, active=active)
    lemmyverse.return_value.json.return_value = [community]

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value = Mock(ap_id='found')
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _preload(client, token, [community])

    assert (subscribe.delay.call_count == 1) is included


def test_a_community_on_a_banned_instance_is_excluded(admin_client, lemmyverse):
    """D924's pin, inverted.

    The route used to test `community['baseurl'] in banned_urls`, a membership
    test against the raw rows. A Mastodon-style wildcard ban is a PATTERN, not
    a domain, so `'evil.com' in ['ev*l.com']` is False and preload would have
    subscribed this instance to communities on an instance it had defederated.
    Both a plain ban and a wildcard ban are asserted, because the plain one
    passed before the fix too.
    """
    client, token = admin_client
    db.session.add_all([BannedInstances(domain='plain.example'),
                        BannedInstances(domain='wild*.example')])
    db.session.commit()
    cache.clear()

    communities = [_community('https://plain.example/c/x', 'plain.example'),
                   _community('https://wildX.example/c/x', 'wildX.example')]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value = None
        _response, flashed = _preload(client, token, communities)

    assert '2 out of 2 communities were excluded using current filters' in \
        ' '.join(_flashes(flashed))
    assert search.call_args_list == []


def test_a_community_whose_name_is_a_bad_word_is_excluded(admin_client,
                                                           lemmyverse):
    client, token = admin_client
    community = _community('https://e.example/c/x', 'e.example', name='badname')
    lemmyverse.return_value.json.return_value = [community]

    with patch('app.admin.routes.is_bad_name', return_value=True):
        with patch('app.admin.routes.search_for_community') as search:
            search.return_value = None
            _response, flashed = _preload(client, token, [community])

    assert '1 out of 1 communities were excluded' in ' '.join(_flashes(flashed))
    assert search.call_args_list == []


def test_the_candidates_are_subscribed_most_active_first(admin_client,
                                                          lemmyverse):
    """`sorted(..., key=users_active_week, reverse=True)` and the `range`
    clamp. Asking for two out of three must take the two BUSIEST, so the order
    is asserted rather than the count."""
    client, token = admin_client
    communities = [
        _community('https://quiet.example/c/x', 'quiet.example', active=600),
        _community('https://busy.example/c/x', 'busy.example', active=5000),
        _community('https://middling.example/c/x', 'middling.example', active=2000),
    ]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value.ap_id = 'found'
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _preload(client, token, communities, communities_num='2')

    assert [call.args[0] for call in search.call_args_list] == [
        '!x@busy.example', '!x@middling.example']
    assert subscribe.delay.call_count == 2
    # admin_preload=True, and the reason is in the route's own comment: without
    # it the subscription uses the admin's alt_profile, and 'Leave' then sends
    # the main user name so the unsubscribe never succeeds.
    assert all(call.kwargs == {'admin_preload': True}
               for call in subscribe.delay.call_args_list)


def test_asking_for_more_communities_than_exist_takes_all_of_them(
        admin_client, lemmyverse):
    """`if communities_to_add > len(parsed_communities_sorted)` -- without the
    clamp the `range` loop would raise IndexError."""
    client, token = admin_client
    communities = [_community('https://only.example/c/x', 'only.example')]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value.ap_id = 'found'
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _preload(client, token, communities, communities_num='500')

    assert subscribe.delay.call_count == 1


def test_a_community_that_cannot_be_found_is_skipped(admin_client, lemmyverse):
    """`if not new_community: continue` -- search_for_community reaches the
    remote instance, so a candidate that passed every filter can still be
    unreachable, and that must not stop the rest."""
    client, token = admin_client
    communities = [_community('https://gone.example/c/x', 'gone.example', active=5000),
                   _community('https://here.example/c/y', 'here.example', active=1000)]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        # The busiest candidate is searched first and is the one not found.
        search.side_effect = [None, Mock(ap_id='here.example/c/y')]
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _preload(client, token, communities)

    assert subscribe.delay.call_count == 1
    assert subscribe.delay.call_args.args == ('here.example/c/y', 1)


def test_the_preload_runs_inline_and_reports_results_in_debug(app, admin_client,
                                                              lemmyverse):
    """`if current_app.debug:` -- debug runs do_subscribe synchronously and
    flashes what it returned, which is the only way an operator sees why a
    subscription failed."""
    client, token = admin_client
    communities = [_community('https://here.example/c/x', 'here.example')]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value = Mock(ap_id='found')
        with patch('app.admin.routes.do_subscribe',
                   return_value='subscribed ok') as subscribe:
            # current_app.debug reads config['DEBUG'], so this is the whole
            # switch -- patching current_app itself would also replace every
            # config lookup the route makes.
            with patch.dict(app.config, {'DEBUG': True}):
                _response, flashed = _preload(client, token, communities)

    assert subscribe.call_args.args == ('found', 1)
    assert subscribe.call_args.kwargs == {'admin_preload': True}
    assert any('subscribed ok' in message for message in _flashes(flashed))


def test_a_preload_asking_for_zero_communities_adds_twenty_five(admin_client,
                                                                lemmyverse):
    """`if preload_form.communities_num.data: ... else: communities_to_add = 25`.

    Zero, not blank. A blank IntegerField is a validation ERROR -- "Not a valid
    integer value" -- so the whole branch is skipped and nothing runs, and an
    absent key keeps the field's `default=25`. The only submission that reaches
    the else arm is an explicit 0, which this route reads as "use the default"
    rather than as "add none".
    """
    client, token = admin_client
    communities = [_community(f'https://n{i}.example/c/x', f'n{i}.example',
                              active=1000 + i) for i in range(30)]
    lemmyverse.return_value.json.return_value = communities

    with patch('app.admin.routes.search_for_community') as search:
        search.return_value.ap_id = 'found'
        with patch('app.admin.routes.do_subscribe') as subscribe:
            _preload(client, token, communities, communities_num='0')

    assert subscribe.delay.call_count == 25


def test_a_refused_federation_post_keeps_what_was_submitted(admin_client):
    """`elif request.method == 'GET':` -- the pre-fill arm must not run on a
    POST. Under the mutant a submission the form REFUSES comes back showing the
    stored blocklist instead of what the admin typed, silently discarding an
    edit to the instance's federation policy. `federation_mode` is a RadioField
    with fixed choices, so an unknown value is the refusal."""
    client, token = admin_client
    db.session.add(BannedInstances(domain='stored.example'))
    db.session.commit()

    _response, render = _post(client, token, federation_mode='nonsense',
                              blocklist='typed.example')

    form = render.call_args.kwargs['form']
    assert 'federation_mode' in form.errors
    assert form.blocklist.data == 'typed.example'
    assert [row.domain for row in BannedInstances.query.all()] == ['stored.example']


def test_the_preload_page_renders_without_a_submission(admin_client):
    """The `return render_template(...)` past the submit branch. Every other
    preload row ends in a redirect, so without this the page an admin actually
    lands on is never rendered."""
    client, token = admin_client

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/federation/preload')

    assert response.status_code == 200
    assert render.call_args.args == ('admin/federation_preload.html',)
    assert render.call_args.kwargs['preload_form'] is not None


# --------------------------------------------------------------------------
# admin_federation_ban_lists
# --------------------------------------------------------------------------


@pytest.fixture
def media_is_clean():
    import glob

    before = set(glob.glob(f'{MEDIA}/*.json'))
    yield
    for path in set(glob.glob(f'{MEDIA}/*.json')) - before:
        os.unlink(path)


def _import(client, token, payload, filename='bans.json'):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        with patch('app.admin.routes.flash') as flashed:
            response = client.post(
                '/admin/federation/ban_lists',
                data={'import_file': (io.BytesIO(body), filename),
                      'import_submit': 'Import', 'csrf_token': token},
                content_type='multipart/form-data')
    return response, flashed


def test_an_import_with_no_file_says_so(admin_client, media_is_clean):
    """The `else` of `if import_file and import_file.filename != ''`."""
    client, token = admin_client

    _response, flashed = _import(client, token, {}, filename='')

    assert _flashes(flashed) == ['Ban imports requested, but no json provided.']


def test_an_import_of_something_that_is_not_json_is_refused(admin_client,
                                                            media_is_clean):
    """The extension check refuses before the file is read. Nothing is written
    to `app/static/media`, which is SERVED (D930)."""
    import glob

    client, token = admin_client

    response, _flashed = _import(client, token, {}, filename='bans.txt')

    assert response.status_code == 400
    assert glob.glob(f'{MEDIA}/*.json') == []


def test_an_import_named_with_an_uppercase_extension_is_accepted(
        admin_client, media_is_clean):
    """`file_ext.lower() != '.json'`. A file chooser on a case-preserving
    filesystem hands over `BANS.JSON`, and refusing it would read as the file
    being malformed rather than as the name being shouted."""
    client, token = admin_client

    with patch('app.admin.routes.import_bans_task') as task:
        response, _flashed = _import(client, token, {'banned_instances': []},
                                     filename='BANS.JSON')

    assert response.status_code == 302
    assert task.delay.call_count == 1


def test_an_import_hands_the_task_the_contents_and_writes_no_file(
        admin_client, media_is_clean):
    """D930, fixed. The upload was saved into `app/static/media`, which is
    SERVED, as `<gibberish>.json.json`, and never deleted -- every import left
    the instance's full ban list there. The route now reads the upload in
    memory and hands its text to the task, so no file is written at all (owner
    ruling 2026-09-30). `import_bans_task` is covered separately below."""
    import glob

    client, token = admin_client
    before = set(glob.glob(f'{MEDIA}/**', recursive=True))

    with patch('app.admin.routes.import_bans_task') as task:
        _response, flashed = _import(client, token, {'banned_instances': ['x.example']})

    assert _flashes(flashed) == ['Ban imports started in a background process.']
    assert json.loads(task.delay.call_args.args[0]) == {'banned_instances': ['x.example']}
    assert set(glob.glob(f'{MEDIA}/**', recursive=True)) == before


def test_an_export_sends_the_blocklist_as_a_json_download(admin_client):
    """The export branch. `use_allowlist` False means the banned list is sent
    and the allowed one is not -- exporting the wrong one would publish the
    opposite of the instance's policy."""
    client, token = admin_client
    db.session.add_all([BannedInstances(domain='banned.example'),
                        AllowedInstances(domain='allowed.example'),
                        Domain(name='baddomain.example', banned=True),
                        Tag(name='badtag', display_as='BadTag', banned=True)])
    db.session.commit()
    banned_user = make_user(make_instance('remote.example'), 'bad', local=False)
    banned_user.banned = True
    db.session.commit()

    response = client.post('/admin/federation/ban_lists',
                           data={'export_submit': 'Export', 'csrf_token': token})

    assert response.status_code == 200
    assert response.mimetype == 'application/json'
    assert 'attachment' in response.headers['Content-Disposition']
    exported = json.loads(response.get_data())
    assert exported['banned_instances'] == ['banned.example']
    assert 'allowed_instances' not in exported
    assert exported['banned_domains'] == ['baddomain.example']
    assert exported['banned_tags'] == [{'name': 'badtag', 'display_as': 'BadTag'}]
    assert exported['banned_users'] == [banned_user.ap_id]


def test_an_export_in_allowlist_mode_sends_the_allowlist_instead(admin_client):
    from app.utils import set_setting

    client, token = admin_client
    set_setting('use_allowlist', True)
    db.session.add_all([BannedInstances(domain='banned.example'),
                        AllowedInstances(domain='allowed.example')])
    db.session.commit()

    response = client.post('/admin/federation/ban_lists',
                           data={'export_submit': 'Export', 'csrf_token': token})

    exported = json.loads(response.get_data())
    assert exported['allowed_instances'] == ['allowed.example']
    assert 'banned_instances' not in exported


def test_an_export_with_nothing_banned_is_still_a_valid_file(admin_client):
    """Every `if len(...) > 0` false arm at once. An empty instance must export
    a file the import side can read, not a file missing its keys -- which is
    what D923 was about on the other end."""
    client, token = admin_client

    response = client.post('/admin/federation/ban_lists',
                           data={'export_submit': 'Export', 'csrf_token': token})

    exported = json.loads(response.get_data())
    assert exported == {'banned_instances': [], 'banned_domains': [],
                        'banned_tags': [], 'banned_users': []}


def test_the_ban_lists_page_renders_without_a_submission(admin_client):
    """The `return render_template(...)` past both submit branches."""
    client, token = admin_client

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/federation/ban_lists')

    assert response.status_code == 200
    assert render.call_args.args == ('admin/federation_ban_lists.html',)


def test_an_import_in_debug_runs_inline_instead_of_queueing(app, admin_client,
                                                            media_is_clean):
    """`if current_app.debug: import_bans_task(final_place)`.

    Debug applies the import synchronously, which is what makes D923 visible to
    an operator: a file missing a section used to raise here, as a 500 over a
    half-applied import, rather than failing quietly in a worker.
    """
    client, token = admin_client

    with patch.dict(app.config, {'DEBUG': True}):
        response, flashed = _import(client, token,
                                    {'banned_instances': ['evil.example']})

    assert response.status_code == 302
    assert '/admin/federation/ban_lists' in response.headers['Location']
    # Inline: the rows are already there when the response comes back, and
    # nothing was flashed about a background process.
    assert [row.domain for row in BannedInstances.query.all()] == ['evil.example']
    assert _flashes(flashed) == []


# --------------------------------------------------------------------------
# import_bans_task
# --------------------------------------------------------------------------


@pytest.fixture
def bans_file():
    """The uploaded file's text, which is what the route hands the task (D930)."""
    def write(payload):
        return json.dumps(payload)
    return write


def test_importing_an_allowlist_actually_imports_it(app, db_session, bans_file):
    """D922's pin, inverted -- the round's sharpest finding.

    The guard read `isinstance(instance_allowed, list)`, and `instance_allowed`
    is the FUNCTION imported at the top of `app/admin/routes.py`. It is never a
    list, so the condition was False for every file ever imported and the
    allowlist import did nothing at all -- silently, because the `and`
    short-circuited before `len()` could raise on a function.

    An instance in allowlist mode is one that federates with nobody it has not
    listed, so a silent no-op here means the operator believes they have
    imported their peers and have not.
    """
    from app.admin.routes import import_bans_task
    from app.utils import set_setting

    _seed()
    set_setting('use_allowlist', True)
    path = bans_file({'allowed_instances': ['friend.example', 'other.example']})

    import_bans_task(path)

    assert sorted(row.domain for row in AllowedInstances.query.all()) == [
        'friend.example', 'other.example']


def test_importing_an_allowlist_skips_what_is_already_allowed(app, db_session,
                                                              bans_file):
    """The `continue`. Without it the import would add a duplicate row on every
    run, and `instance_allowed` would keep answering from whichever it found
    first."""
    from app.admin.routes import import_bans_task
    from app.utils import set_setting

    _seed()
    set_setting('use_allowlist', True)
    db.session.add(AllowedInstances(domain='friend.example'))
    db.session.commit()

    import_bans_task(bans_file({'allowed_instances': ['friend.example',
                                                      'new.example']}))

    assert sorted(row.domain for row in AllowedInstances.query.all()) == [
        'friend.example', 'new.example']


def test_importing_a_blocklist_adds_the_instances(app, db_session, bans_file):
    from app.admin.routes import import_bans_task

    _seed()

    import_bans_task(bans_file({'banned_instances': ['evil.example',
                                                     'spam.example']}))

    assert sorted(row.domain for row in BannedInstances.query.all()) == [
        'evil.example', 'spam.example']


def test_importing_a_blocklist_skips_what_is_already_banned(app, db_session,
                                                            bans_file):
    from app.admin.routes import import_bans_task

    _seed()
    db.session.add(BannedInstances(domain='evil.example'))
    db.session.commit()

    import_bans_task(bans_file({'banned_instances': ['evil.example',
                                                     'new.example']}))

    assert sorted(row.domain for row in BannedInstances.query.all()) == [
        'evil.example', 'new.example']


def test_importing_domains_tags_and_users(app, db_session, bans_file):
    """The three sections that run in both federation modes."""
    from app.admin.routes import import_bans_task

    _seed()

    import_bans_task(bans_file({
        'banned_instances': [],
        'banned_domains': ['bad.example'],
        'banned_tags': [{'name': 'badtag', 'display_as': 'BadTag'}],
        'banned_users': ['someone@remote.example'],
    }))

    assert [row.name for row in Domain.query.filter_by(banned=True)] == ['bad.example']
    tag = Tag.query.filter_by(banned=True).one()
    assert (tag.name, tag.display_as) == ('badtag', 'BadTag')
    user = User.query.filter_by(banned=True).one()
    assert (user.user_name, user.ap_id) == ('someone', 'someone@remote.example')


def test_importing_skips_domains_tags_and_users_already_banned(app, db_session,
                                                                bans_file):
    """The three remaining `continue`s, each of which prevents a duplicate row
    for something already banned."""
    from app.admin.routes import import_bans_task

    instance, ordinary = _seed()
    existing_user = make_user(make_instance('remote.example'), 'someone',
                              local=False)
    existing_user.banned = True
    db.session.add_all([Domain(name='bad.example', banned=True),
                        Tag(name='badtag', display_as='BadTag', banned=True)])
    db.session.commit()

    import_bans_task(bans_file({
        'banned_instances': [],
        'banned_domains': ['bad.example'],
        'banned_tags': [{'name': 'badtag', 'display_as': 'BadTag'}],
        'banned_users': [existing_user.ap_id],
    }))

    assert Domain.query.filter_by(banned=True).count() == 1
    assert Tag.query.filter_by(banned=True).count() == 1
    assert User.query.filter_by(banned=True).count() == 1


def test_a_file_missing_a_section_imports_the_rest(app, db_session, bans_file):
    """D923's pin, inverted.

    Each section commits on its own, so `contents_json['banned_users']` raising
    on a file without that key left the database PARTLY updated and raised --
    and in debug mode the route calls this synchronously, so the admin got a
    500 over a half-applied import. A section with nothing to import is now a
    no-op.
    """
    from app.admin.routes import import_bans_task

    _seed()

    import_bans_task(bans_file({'banned_instances': ['evil.example']}))

    assert [row.domain for row in BannedInstances.query.all()] == ['evil.example']
    assert Domain.query.filter_by(banned=True).count() == 0
    assert User.query.filter_by(banned=True).count() == 0


def test_a_section_that_is_not_a_list_is_ignored(app, db_session, bans_file):
    """`isinstance(..., list)`. The file is admin-supplied but arrives from
    another instance's export, so a section of the wrong shape must be skipped
    rather than iterated."""
    from app.admin.routes import import_bans_task

    _seed()

    import_bans_task(bans_file({'banned_instances': 'evil.example',
                                'banned_domains': {'bad': True}}))

    assert BannedInstances.query.count() == 0
    assert Domain.query.filter_by(banned=True).count() == 0


def test_an_empty_section_is_ignored(app, db_session, bans_file):
    """`len(...) > 0` -- the false arm of each guard."""
    from app.admin.routes import import_bans_task

    _seed()

    import_bans_task(bans_file({'banned_instances': [], 'banned_domains': [],
                                'banned_tags': [], 'banned_users': []}))

    assert BannedInstances.query.count() == 0


def test_a_failed_import_rolls_back_and_re_raises(app, db_session):
    """`except Exception: session.rollback(); raise`. The task holds its own
    session, so a failure that did not roll back would leave it poisoned for
    whatever ran next on that worker."""
    from app.admin.routes import import_bans_task

    _seed()

    with pytest.raises(ValueError):
        import_bans_task('{"banned_instances": ["evil.example"]')  # truncated JSON

    assert BannedInstances.query.count() == 0
