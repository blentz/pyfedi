"""ATProto P2: Bluesky accounts PieFed hosts (spec 2026-11-23-atproto-federation-design.md §8, §9, §10; A8-A10).

tests/atproto_fake.py plays the PDS. Each test gets its own Redis keys for the budget, the describeServer cache and
the creation spacing, since the test Redis is shared between workers.
"""
import itertools
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import db, storefront
from app.atproto import budget, records
from app.clock import utcnow
from app.connections import AuthFailed, Permanent, Progress, QuotaWait, SERVICES, Transient, setting_name
from app.connections import bluesky
from app.connections import bluesky_hosted as hosted
from app.connections.apps import set_domain_switch
from app.connections.dispatch import offered_connections, perform, purge_user, record_choices
from app.connections.tokens import open_tokens, seal_tokens
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE
from app.models import Notification, SocialConnection, SocialCrosspost, UserFollower
from app.sealing import generate_key
from app.utils import set_setting
from tests.atproto_fake import PDS
from tests.factories import make_post
from tests.shared_fixtures.atproto import a_hosted, a_member, a_microblog, follow

_numbers = itertools.count(1)


# --------------------------------------------------------------------------
# the budget (§9)
# --------------------------------------------------------------------------

def test_the_budget_keeps_room_for_what_matters_most(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_BUDGET_SECOND', 4)
    monkeypatch.setattr(budget.time, 'time', lambda: 1_000_000.25)
    assert budget.take('backfill') is None and budget.take('backfill') is None   # half of four
    assert budget.take('backfill') == 1                                         # full for backfill: wait a second
    assert budget.take('delete') is None and budget.take('delete') is None
    assert budget.take('delete') == 1
    import app as app_pkg
    assert int(app_pkg.redis_client.get(f'{budget.PREFIX}:3600:{1_000_000 // 3600}')) == 4   # refused takes undone


def test_a_full_day_waits_until_the_next(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_BUDGET_DAY', 1)
    monkeypatch.setattr(budget.time, 'time', lambda: 86400 * 20 + 86000.5)
    assert budget.take('delete') is None
    assert budget.take('delete') == 399


# --------------------------------------------------------------------------
# the PDS, sessions
# --------------------------------------------------------------------------

def test_configured_only_with_address_and_admin_password(hosted_world, monkeypatch):
    assert hosted.configured()
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_PDS_ADMIN_PASSWORD', '')
    assert not hosted.configured()


def test_describe_is_kept_and_its_domains_read(hosted_world):
    assert hosted.handle_domains() == [hosted_world.home]
    assert hosted.describe()['did'] == 'did:web:pds.example'
    assert hosted_world.pds.calls.count('com.atproto.server.describeServer') == 1


def test_an_unreachable_pds_names_no_domain(hosted_world):
    hosted_world.pds.fail['com.atproto.server.describeServer'] = httpx.Response(502)
    assert hosted.handle_domains() == []


def test_a_session_answer_becomes_tokens(hosted_world):
    did = hosted_world.pds.account('x.example')
    tokens = hosted._tokens(hosted_world.pds.session(did, lifetime=100), 'pw')
    assert tokens['kind'] == 'hosted' and tokens['pds'] == PDS and 90 < tokens['expires_at'] - time.time() <= 100
    assert 590 < hosted._expiry('not a jwt') - time.time() <= 600


def test_a_refresh_uses_the_refresh_token(hosted_world):
    did = hosted_world.pds.account('x.example', password='secret')
    tokens = hosted_world.pds.tokens(did)
    fresh = hosted.refresh(tokens)
    assert fresh['access_token'] != tokens['access_token'] and fresh['password'] == 'secret'
    assert hosted_world.pds.calls[-1] == 'com.atproto.server.refreshSession'


def test_a_dead_refresh_token_signs_in_again(hosted_world):
    did = hosted_world.pds.account('x.example', password='secret')
    tokens = {**hosted_world.pds.tokens(did), 'refresh_token': 'gone'}
    assert hosted.refresh(tokens)['did'] == did and hosted_world.pds.calls[-1] == 'com.atproto.server.createSession'


def test_a_lost_password_is_set_again_by_the_admin(hosted_world):
    did = hosted_world.pds.account('x.example', password='new-elsewhere')
    tokens = {**hosted_world.pds.tokens(did), 'refresh_token': 'gone', 'password': 'old'}
    fresh = hosted.refresh(tokens)
    assert fresh['password'] != 'old' and hosted_world.pds.accounts[did]['password'] == fresh['password']
    assert 'com.atproto.admin.updateAccountPassword' in hosted_world.pds.calls


def test_an_account_the_pds_lost_ends_the_connection(hosted_world):
    with pytest.raises(AuthFailed):
        hosted.sign_in('did:plc:nobody', 'pw')


def test_a_refresh_when_the_pds_is_down_waits(hosted_world):
    did = hosted_world.pds.account('x.example')
    hosted_world.pds.fail['com.atproto.server.refreshSession'] = httpx.Response(503)
    with pytest.raises(Transient):
        hosted.refresh(hosted_world.pds.tokens(did))


def test_the_service_refreshes_a_hosted_account_its_way(hosted_world):
    did = hosted_world.pds.account('x.example')
    assert bluesky.SERVICE.refresh(hosted_world.pds.tokens(did))['kind'] == 'hosted'


def test_a_hosted_session_calls_with_its_access_token(hosted_world):
    did = hosted_world.pds.account('x.example')
    session = hosted.HostedSession(hosted_world.pds.tokens(did))
    assert session.did == did and session.pds == PDS
    assert session.xrpc('POST', 'com.atproto.server.createAppPassword', json={'name': 'n'}).json()['password']
    hosted_world.pds.fail['com.atproto.repo.uploadBlob'] = httpx.Response(400, json={'error': 'ExpiredToken'})
    with pytest.raises(Transient):
        session.xrpc('POST', 'com.atproto.repo.uploadBlob', content=b'x')
    with pytest.raises(AuthFailed):
        hosted.HostedSession({**hosted_world.pds.tokens(did), 'access_token': 'nope'}).xrpc(
            'POST', 'com.atproto.repo.uploadBlob', content=b'x')


def test_a_hosted_session_streams_a_video(hosted_world, tmp_path):
    did = hosted_world.pds.account('x.example')
    path = tmp_path / 'v.mp4'
    path.write_bytes(b'\0' * 10)
    with open(path, 'rb') as video:
        video.read(4)
        blob = hosted.HostedSession(hosted_world.pds.tokens(did)).xrpc(
            'POST', 'com.atproto.repo.uploadBlob', stream=video, headers={'Content-Type': 'video/mp4'}).json()['blob']
    assert blob['mimeType'] == 'video/mp4'


def test_spending_past_the_budget_waits_without_an_attempt(hosted_world, monkeypatch):
    monkeypatch.setattr(budget, 'take', lambda kind: 30)
    with pytest.raises(QuotaWait) as wait:
        hosted.spend('post')
    assert timedelta(seconds=29) < wait.value.until - utcnow() <= timedelta(seconds=30)


# --------------------------------------------------------------------------
# who may create one, and which handle (A3, A9)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('name, label', [('Alice_Smith', 'alice-smith'), ('a', 'a-pf'), ('', 'member'),
                                         ('__x__', 'x-pf'), ('Émile.Zola', 'mile-zola'),
                                         ('abcdefghijklmnopq-rstuv', 'abcdefghijklmnopq'), ('a--b', 'a-b')])
def test_a_name_as_a_handle_label(name, label):
    assert hosted.label_of(name) == label


def test_the_next_label_to_offer(hosted_world):
    assert hosted.alternative('alice') == 'alice-2'
    assert hosted.alternative('abcdefghijklmnopqr', 10) == 'abcdefghijklmno-10'
    alice = SimpleNamespace(user_name='Alice', id=0, site_domain_id=hosted_world.author.site_domain_id)
    bob2 = SimpleNamespace(user_name='bob_2', id=0, site_domain_id=hosted_world.author.site_domain_id)
    assert hosted.next_label(alice, 'alice') == 'alice-2' and hosted.next_label(alice, 'alice-2') == 'alice-3'
    assert hosted.next_label(bob2, 'bob-2') == 'bob-2-2'   # a name that ends in a number keeps it
    assert hosted.next_label(alice, 'someone') == 'alice-2'
    with pytest.raises(hosted.HandleRefused):
        hosted.next_label(alice, f'alice-{hosted.MAX_NUMBER}')


def test_a_member_may_have_only_their_own_name(hosted_world):
    alice = SimpleNamespace(user_name='Alice_Smith', id=0, site_domain_id=hosted_world.author.site_domain_id)
    assert hosted.own_label(alice) == hosted.own_label(alice, '  ') == 'alice-smith'
    assert hosted.own_label(alice, 'Alice-Smith-7') == 'alice-smith-7'
    for taken in ('admin', 'bob', 'alice-smith-1', 'alice-smith-100', 'alice-smith-x', 'xalice-smith-2'):
        with pytest.raises(hosted.HandleRefused) as refused:
            hosted.own_label(alice, taken)
        assert refused.value.suggestion == 'alice-smith'


def test_a_name_the_site_keeps_is_nobodys(hosted_world, monkeypatch):
    """pay.<domain> answers for the site's own payments: a member called pay is offered pay-2."""
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_RESERVED_LABELS', ' Pay, www,,pay-2 ')
    assert hosted.reserved_labels() == {'pay', 'www', 'pay-2'}
    pay = SimpleNamespace(user_name='pay', id=0, site_domain_id=hosted_world.author.site_domain_id)
    for asked in (None, 'pay', 'nothing-of-mine'):
        with pytest.raises(hosted.HandleRefused) as refused:
            hosted.own_label(pay, asked)
        assert refused.value.suggestion == 'pay-3'   # pay-2 is the site's too
    assert hosted.own_label(pay, 'pay-3') == 'pay-3'
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_RESERVED_LABELS', '')
    assert hosted.reserved_labels() == set() and hosted.own_label(pay) == 'pay'


def test_hosting_is_on_only_when_everything_says_so(hosted_world, monkeypatch):
    domain_id = hosted_world.author.site_domain_id
    assert hosted.hosting_on(domain_id)
    set_setting(setting_name(hosted.HOSTING), False)
    assert not hosted.hosting_on(domain_id)
    real = storefront.domain_by_id(domain_id)
    if real.id is not None:
        set_domain_switch(hosted.HOSTING, real.id, 'on')
        assert hosted.hosting_on(domain_id)
        set_domain_switch(hosted.HOSTING, real.id, 'off')
        set_setting(setting_name(hosted.HOSTING), True)
        assert not hosted.hosting_on(domain_id)
        set_domain_switch(hosted.HOSTING, real.id, None)
    set_setting(setting_name(hosted.HOSTING), True)
    for change in ({'federates': False}, {'is_onion': True}, {'domain': 'elsewhere.example'}):
        monkeypatch.setattr(hosted.storefront, 'domain_by_id', lambda _id, change=change: real._replace(**change))
        assert not hosted.hosting_on(domain_id)
    monkeypatch.undo()


def test_hosting_is_off_without_the_pds_or_bluesky(hosted_world, monkeypatch):
    set_setting(setting_name('bluesky'), False)
    assert not hosted.hosting_on(hosted_world.author.site_domain_id)
    set_setting(setting_name('bluesky'), True)
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_PDS_URL', '')
    assert not hosted.hosting_on(hosted_world.author.site_domain_id)


def test_a_domain_switch_for_a_domain_with_no_row(hosted_world):
    assert hosted.hosting_on(None) == hosted.hosting_on(hosted_world.author.site_domain_id)


# --------------------------------------------------------------------------
# creating (§8, A8, A9)
# --------------------------------------------------------------------------

def test_creating_makes_the_account_its_identity_and_sends_profile_and_posts(hosted_world):
    old = a_microblog(hosted_world)
    elsewhere = make_post(hosted_world.community, hosted_world.author, f'https://{hosted_world.home}/post/e1', title='not a microblog')
    oauth = SocialConnection(user_id=hosted_world.author.id, service='bluesky', variant='oauth', remote_id='did:plc:own',
                             remote_name='me.bsky.social', sealed_tokens=seal_tokens({}), is_identity=True)
    db.session.add(oauth)
    db.session.commit()
    connection = hosted.request_account(hosted_world.author)
    assert (connection.slot_state, connection.is_identity, connection.import_own, connection.default_on) == \
        (hosted.ACTIVE, True, True, False)
    assert db.session.get(SocialConnection, oauth.id).is_identity is False
    did = connection.remote_id
    assert hosted_world.pds.accounts[did]['handle'] == f'author.{hosted_world.home}' == connection.remote_name
    assert open_tokens(connection)['password'] == hosted_world.pds.accounts[did]['password']
    profile = hosted_world.pds.records[f'at://{did}/app.bsky.actor.profile/self']
    assert profile['displayName'] == 'author' and profile['description'].endswith('/u/author')
    rows = SocialCrosspost.query.filter_by(connection_id=connection.id).all()
    assert [(row.post_id, row.state, row.progress) for row in rows] == [(old.id, 'queued', {'backfill': True})]
    assert elsewhere.id not in [row.post_id for row in rows]
    assert hosted.request_account(hosted_world.author).id == connection.id   # asked again: the same one


def test_an_invite_is_minted_when_the_pds_wants_one(hosted_world):
    hosted_world.pds.invite = True
    connection = hosted.request_account(hosted_world.author, 'author-3')
    assert connection.remote_name == f'author-3.{hosted_world.home}' and not hosted_world.pds.invites
    assert 'com.atproto.server.createInviteCode' in hosted_world.pds.calls


def test_another_name_is_refused_before_the_pds_is_asked(hosted_world):
    with pytest.raises(hosted.HandleRefused) as refused:
        hosted.request_account(hosted_world.author, 'admin')
    assert refused.value.suggestion == 'author' and not hosted_world.pds.calls


def test_the_email_reaches_nobody(hosted_world):
    connection = hosted.request_account(hosted_world.author)
    assert connection.slot_state == hosted.ACTIVE
    request = next(call for call in hosted_world.pds.requests if call.url.path.endswith('createAccount'))
    email = json.loads(request.content)['email']
    local, domain = email.split('@')
    assert domain == 'accounts.invalid' and len(local) == 24 and int(local, 16) >= 0   # random, under no real domain


def test_a_handle_that_is_taken_offers_the_next(hosted_world):
    hosted_world.pds.taken.add(f'author.{hosted_world.home}')
    with pytest.raises(hosted.HandleRefused) as refused:
        hosted.request_account(hosted_world.author)
    assert refused.value.suggestion == 'author-2' and SocialConnection.query.count() == 0


def test_creations_are_spaced(hosted_world, monkeypatch):
    import app as app_pkg
    monkeypatch.setattr(hosted, 'CREATE_SPACING_MS', 60_000)
    monkeypatch.setattr(hosted.time, 'sleep', lambda seconds: None)
    app_pkg.redis_client.set(hosted.CREATE_KEY, '1', px=60_000)
    with pytest.raises(Transient):
        hosted.request_account(hosted_world.author)


def test_a_full_house_puts_the_member_on_the_waiting_list(hosted_world):
    for name in ('first', 'second'):
        a_hosted(hosted_world, a_member(name))
    connection = hosted.request_account(hosted_world.author)
    assert (connection.slot_state, connection.remote_id) == (hosted.WAITING, f'waiting:{hosted_world.author.id}')
    assert open_tokens(connection) == {'label': 'author'} and hosted.place_of(connection) == 1
    assert not hosted_world.pds.records


def test_a_full_server_waits_too(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_HOSTED_LIMIT', 1)
    a_hosted(hosted_world, a_member('gone'), state=hosted.DEACTIVATED)
    assert hosted.slot_free(new_account=False) and not hosted.slot_free(new_account=True)
    assert hosted.request_account(hosted_world.author).slot_state == hosted.WAITING


def test_the_waiting_list_goes_by_followers_then_joining(hosted_world):
    early, popular, late = a_member('early'), a_member('popular'), a_member('late')
    follow(popular, 2)
    rows = [a_hosted(hosted_world, user, state=hosted.WAITING) for user in (late, early, popular)]
    early.created, late.created = utcnow() - timedelta(days=30), utcnow()
    db.session.commit()
    assert [row.user_id for row in hosted.waiting_list()] == [popular.id, early.id, late.id]
    assert hosted.place_of(rows[0]) == 3 and hosted.followers(popular.id) == 2


# --------------------------------------------------------------------------
# turning off and on, deleting, renaming, app passwords (§8)
# --------------------------------------------------------------------------

def test_turning_off_and_on(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    hosted.deactivate(connection)
    assert connection.slot_state == hosted.DEACTIVATED and not hosted_world.pds.accounts[connection.remote_id]['active']
    hosted.reactivate(connection)
    assert connection.slot_state == hosted.ACTIVE and hosted_world.pds.accounts[connection.remote_id]['active']
    assert Notification.query.filter_by(user_id=hosted_world.author.id, subtype='bluesky_slot').count() == 1


def test_turning_on_with_no_free_slot_waits(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.DEACTIVATED)
    for name in ('first', 'second'):
        a_hosted(hosted_world, a_member(name))
    hosted.reactivate(connection)
    assert connection.slot_state == hosted.WAITING
    assert hosted.request_account(hosted_world.author).id == connection.id   # Create again: still waiting


def test_creating_again_turns_a_turned_off_account_on(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.DEACTIVATED)
    assert hosted.request_account(hosted_world.author).slot_state == hosted.ACTIVE == connection.slot_state


def test_turning_on_when_the_pds_is_down(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.DEACTIVATED)
    hosted_world.pds.fail['com.atproto.server.createSession'] = httpx.Response(503)
    with pytest.raises(Transient):
        hosted.reactivate(connection)


def test_deleting_removes_the_account(hosted_world, monkeypatch):
    connection = a_hosted(hosted_world, hosted_world.author)
    did = connection.remote_id
    hosted.delete_account(connection)
    assert did not in hosted_world.pds.accounts and SocialConnection.query.count() == 0
    waiting = a_hosted(hosted_world, hosted_world.author, state=hosted.WAITING)
    calls = len(hosted_world.pds.calls)
    hosted.delete_account(waiting)   # no account yet: nothing to ask
    assert len(hosted_world.pds.calls) == calls and SocialConnection.query.count() == 0


def test_a_delete_the_pds_refuses_is_kept_and_tried_daily(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, is_identity=True, slot_pinned=True)
    did = connection.remote_id
    hosted_world.pds.fail['com.atproto.admin.deleteAccount'] = httpx.Response(500)
    assert hosted.delete_account(connection) is False
    assert (connection.slot_state, connection.slot_pinned, connection.is_identity) == (hosted.DELETING, False, False)
    assert open_tokens(connection)['password']   # still the way into the account
    assert not bluesky.SERVICE.sendable(connection) and hosted.active_count() == 0
    assert 'Being deleted' in hosted_world.client.get('/user/settings/connections').data.decode()
    hosted_world.pds.fail['com.atproto.admin.deleteAccount'] = httpx.Response(500)
    with pytest.raises(Transient):   # Create while it is going: the delete is tried once more, then it waits
        hosted.request_account(hosted_world.author)
    assert hosted.daily()['deleted'] == 1
    assert did not in hosted_world.pds.accounts and SocialConnection.query.count() == 0


def test_create_after_a_delete_that_finally_works(hosted_world):
    old = a_hosted(hosted_world, hosted_world.author, state=hosted.DELETING)
    old_did = old.remote_id
    connection = hosted.request_account(hosted_world.author)
    assert connection.slot_state == hosted.ACTIVE and old_did not in hosted_world.pds.accounts


def test_a_delete_without_the_pds_waits_for_it(hosted_world, monkeypatch):
    connection = a_hosted(hosted_world, hosted_world.author)
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_PDS_URL', '')
    assert hosted.delete_account(connection) is False and connection.slot_state == hosted.DELETING


def test_renaming(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    assert hosted.rename(connection, 'Author-2') == f'author-2.{hosted_world.home}' == connection.remote_name
    assert hosted_world.pds.accounts[connection.remote_id]['handle'] == connection.remote_name
    hosted_world.pds.taken.add(f'author-3.{hosted_world.home}')
    with pytest.raises(hosted.HandleRefused) as refused:
        hosted.rename(connection, 'author-3')
    assert refused.value.suggestion == 'author-4'
    with pytest.raises(hosted.HandleRefused) as refused:
        hosted.rename(connection, 'admin')   # nobody else's name
    assert refused.value.suggestion == 'author'
    hosted_world.pds.fail['com.atproto.identity.updateHandle'] = httpx.Response(500)
    with pytest.raises(Transient):
        hosted.rename(connection, 'author')


def test_an_app_password(hosted_world):
    assert hosted.app_password(a_hosted(hosted_world, hosted_world.author)) == 'abcd-efgh-ijkl-mnop'


# --------------------------------------------------------------------------
# the profile
# --------------------------------------------------------------------------

def test_the_profile_carries_name_bio_link_and_pictures(hosted_world, tmp_path):
    from PIL import Image
    path = tmp_path / 'a.png'
    Image.new('RGB', (20, 20), 'blue').save(path)
    picture = SimpleNamespace(file_path=str(path), source_url=None)
    user = SimpleNamespace(title='Alice', user_name='alice', about='I [write](https://w.example) ' + 'x' * 300,
                           avatar=picture, cover=None, public_url=lambda: 'https://piefed.example/u/alice')
    did = hosted_world.pds.account('x.example')
    record = hosted.profile_record(user, hosted.HostedSession(hosted_world.pds.tokens(did)))
    assert record['displayName'] == 'Alice' and len(record['description']) <= 256
    assert record['description'].startswith('I write https://w.example') and \
        record['description'].endswith('\n\nhttps://piefed.example/u/alice')
    assert record['avatar']['mimeType'] == 'image/jpeg' and 'banner' not in record
    bare = hosted.profile_record(SimpleNamespace(title=None, user_name='bob', about=None, avatar=None, cover=None,
                                                 public_url=lambda: 'https://p.example/u/bob'), None)
    assert bare['description'] == 'https://p.example/u/bob' and bare['displayName'] == 'bob'


def test_a_profile_waits_for_the_budget_and_skips_an_inactive_account(hosted_world, monkeypatch):
    assert hosted.put_profile(hosted_world.author.id) is None   # no account: nothing to do
    connection = a_hosted(hosted_world, hosted_world.author)
    monkeypatch.setattr(budget, 'take', lambda kind: 12)
    assert hosted.put_profile(hosted_world.author.id) == 12
    assert f'at://{connection.remote_id}/app.bsky.actor.profile/self' not in hosted_world.pds.records


def test_the_profile_task_retries_after_the_wait_and_gives_up_on_failure(hosted_world, monkeypatch):
    from celery.exceptions import Retry
    monkeypatch.setattr(hosted, 'put_profile', lambda user_id: 7)
    with pytest.raises(Retry) as retry:   # eager here; a worker runs it again after the wait
        hosted.profile_task.apply(args=(hosted_world.author.id,))
    assert retry.value.when == 7
    monkeypatch.setattr(hosted, 'put_profile', lambda user_id: None)
    hosted.profile_task.apply(args=(hosted_world.author.id,))

    def broken(user_id):
        raise Permanent('no')
    monkeypatch.setattr(hosted, 'put_profile', broken)
    hosted.profile_task.apply(args=(hosted_world.author.id,))   # logged, not raised


def test_saving_a_profile_sends_it_to_an_active_account(hosted_world, monkeypatch):
    sent = []
    monkeypatch.setattr(hosted, 'put_profile_later', sent.append)
    hosted.profile_changed(hosted_world.author)
    a_hosted(hosted_world, hosted_world.author)
    hosted.profile_changed(hosted_world.author)
    assert sent == [hosted_world.author.id]


def test_the_profile_task_runs_inline_in_debug(hosted_world, monkeypatch):
    ran = []
    monkeypatch.setattr(hosted_world.app, 'debug', True)
    monkeypatch.setattr(hosted.profile_task, 'apply', lambda args: ran.append(args))
    hosted.put_profile_later(hosted_world.author.id)
    assert ran == [(hosted_world.author.id,)]


def test_the_edit_profile_page_tells_bluesky(hosted_world, monkeypatch):
    from tests.test_user_profile_import import profile_payload
    sent = []
    monkeypatch.setattr('app.user.routes.bluesky_profile_changed', lambda user: sent.append(user.id))
    response = hosted_world.client.post(f'/u/{hosted_world.author.user_name}/profile', content_type='multipart/form-data',
                                 data=profile_payload(hosted_world.token, email=f'{hosted_world.author.user_name}@example.com'))
    assert response.status_code == 302 and sent == [hosted_world.author.id]


# --------------------------------------------------------------------------
# copies through a hosted account (§6, §9)
# --------------------------------------------------------------------------

def test_a_copy_goes_through_the_hosted_account(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    post = a_microblog(hosted_world)
    tokens = open_tokens(connection)
    sent = bluesky.SERVICE.send(post, tokens, connection, Progress())
    record = hosted_world.pds.records[sent.remote_id]
    assert record['text'] == post.body and sent.remote_url.startswith('https://bsky.app/profile/')
    bluesky.SERVICE.delete(tokens, sent.remote_id)
    assert sent.remote_id not in hosted_world.pds.records


def test_a_backfilled_copy_keeps_its_time_and_takes_the_last_turn(hosted_world, monkeypatch):
    connection = a_hosted(hosted_world, hosted_world.author)
    post = a_microblog(hosted_world, posted_at=utcnow() - timedelta(days=3))
    kinds = []
    monkeypatch.setattr(budget, 'take', lambda kind: kinds.append(kind))
    sent = bluesky.SERVICE.send(post, open_tokens(connection), connection, Progress({'backfill': True}))
    assert kinds == ['backfill']
    assert hosted_world.pds.records[sent.remote_id]['createdAt'] == records.stamp(post.posted_at)
    assert records.stamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))) == '2026-01-02T01:04:05Z'


def test_a_copy_past_the_budget_waits_at_no_cost(hosted_world, monkeypatch):
    connection = a_hosted(hosted_world, hosted_world.author)
    post = a_microblog(hosted_world)
    row = SocialCrosspost(post_id=post.id, connection_id=connection.id, service='bluesky', variant=hosted.HOSTED,
                          state='queued', next_attempt_at=utcnow())
    db.session.add(row)
    db.session.commit()
    monkeypatch.setattr(budget, 'take', lambda kind: 60)
    perform(row.id)
    row = db.session.get(SocialCrosspost, row.id)
    assert row.state == 'queued' and row.attempts == 0 and row.next_attempt_at > utcnow() + timedelta(seconds=50)


def test_only_an_active_hosted_account_is_offered_and_sent_to(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.WAITING)
    assert offered_connections(hosted_world.author, POST_TYPE_ARTICLE) == []
    post = a_microblog(hosted_world)
    record_choices(post, None)
    db.session.commit()
    assert SocialCrosspost.query.count() == 0
    row = SocialCrosspost(post_id=post.id, connection_id=connection.id, service='bluesky', variant=hosted.HOSTED,
                          state='queued', next_attempt_at=utcnow())
    db.session.add(row)
    db.session.commit()
    perform(row.id)
    row = db.session.get(SocialCrosspost, row.id)
    assert (row.state, row.last_error) == ('failed', 'This account is not active just now.')
    connection.slot_state = hosted.ACTIVE
    db.session.commit()
    assert [c.id for c, _service in offered_connections(hosted_world.author, POST_TYPE_ARTICLE)] == [connection.id]


def test_purging_a_member_deletes_their_hosted_account(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    did = connection.remote_id
    purge_user(hosted_world.author)
    assert did not in hosted_world.pds.accounts and SocialConnection.query.count() == 0


def test_the_service_kinds_of_a_hosted_account(hosted_world):
    service = SERVICES['bluesky']
    assert service.kind_of('hosted') == 'bluesky_hosted' and service.kind_of('oauth') == 'bluesky'
    assert service.configured()   # the PDS alone is enough
    assert service.sendable(SimpleNamespace(variant='oauth', slot_state=None))
    with pytest.raises(Permanent):   # connecting an own account needs the key
        service.authorize_url(None, 's', 'r', {})


# --------------------------------------------------------------------------
# the daily task (§9)
# --------------------------------------------------------------------------

def test_daily_does_nothing_without_the_pds(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_PDS_URL', '')
    assert hosted.daily() == {'deleted': 0, 'freed': 0, 'granted': 0}


def test_sticky_frees_a_quiet_slot_and_fills_from_the_list(hosted_world, monkeypatch):
    quiet, busy, kept, waiting = (a_member(name) for name in ('quiet', 'busy', 'kept', 'waiter'))
    monkey_old = utcnow() - timedelta(days=100)
    quiet_row = a_hosted(hosted_world, quiet, created_at=monkey_old)
    busy_row = a_hosted(hosted_world, busy, created_at=monkey_old)
    post = a_microblog(hosted_world, busy)
    db.session.add(SocialCrosspost(post_id=post.id, connection_id=busy_row.id, service='bluesky', state='sent',
                                   sent_at=utcnow() - timedelta(days=1)))
    a_hosted(hosted_world, kept, created_at=monkey_old, slot_pinned=True)
    waiting_row = a_hosted(hosted_world, waiting, state=hosted.WAITING)
    db.session.commit()
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_MAX_ACCOUNTS', 3)
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_HOSTED_LIMIT', 5)
    assert hosted.daily() == {'deleted': 0, 'freed': 1, 'granted': 1}
    assert quiet_row.slot_state == hosted.DEACTIVATED and not hosted_world.pds.accounts[quiet_row.remote_id]['active']
    assert Notification.query.filter_by(user_id=quiet.id, subtype='bluesky_slot_freed').count() == 1
    assert waiting_row.slot_state == hosted.ACTIVE and waiting_row.remote_id.startswith('did:')
    assert Notification.query.filter_by(user_id=waiting.id, subtype='bluesky_slot').count() == 1


def test_ranked_keeps_the_top_by_followers(hosted_world, monkeypatch):
    low, high, pinned = a_member('low'), a_member('high'), a_member('pinned')
    follow(high, 3)
    low_row = a_hosted(hosted_world, low)
    a_hosted(hosted_world, pinned, slot_pinned=True)
    high_row = a_hosted(hosted_world, high, state=hosted.WAITING)
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_SLOT_POLICY', 'ranked')
    assert hosted.daily() == {'deleted': 0, 'freed': 1, 'granted': 1}
    assert (low_row.slot_state, high_row.slot_state) == (hosted.WAITING, hosted.ACTIVE)


def test_a_full_server_admits_only_who_has_an_account(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_MAX_ACCOUNTS', 3)
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_HOSTED_LIMIT', 2)
    a_hosted(hosted_world, a_member('one'))
    fresh = a_hosted(hosted_world, a_member('fresh'), state=hosted.WAITING)
    follow(fresh.user, 1)
    back = a_hosted(hosted_world, a_member('back'), state=hosted.WAITING, with_account=True)
    assert hosted.daily() == {'deleted': 0, 'freed': 0, 'granted': 1}
    assert (fresh.slot_state, back.slot_state) == (hosted.WAITING, hosted.ACTIVE)


def test_a_slot_that_cannot_be_freed_or_given_is_tried_tomorrow(hosted_world, monkeypatch):
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_SLOT_POLICY', 'ranked')
    monkeypatch.setitem(hosted_world.app.config, 'ATPROTO_MAX_ACCOUNTS', 1)
    stuck = a_hosted(hosted_world, a_member('stuck'))
    hosted_world.pds.accounts[stuck.remote_id]['password'] = 'changed'   # its session still works
    rising = a_hosted(hosted_world, a_member('rising'), state=hosted.WAITING)
    follow(rising.user, 2)
    hosted_world.pds.fail['com.atproto.server.deactivateAccount'] = httpx.Response(503)
    assert hosted.daily() == {'deleted': 0, 'freed': 0, 'granted': 0}
    assert stuck.slot_state == hosted.ACTIVE and rising.slot_state == hosted.WAITING


def test_a_grant_that_finds_every_handle_taken(hosted_world):
    row = a_hosted(hosted_world, a_member('dup'), state=hosted.WAITING)
    hosted_world.pds.taken.update({f'dup.{hosted_world.home}'} | {f'dup-{n}.{hosted_world.home}' for n in range(2, 7)})
    assert hosted.grant(row) is False and row.slot_state == hosted.WAITING
    hosted_world.pds.taken.clear()
    hosted_world.pds.fail['com.atproto.server.createAccount'] = httpx.Response(500)
    assert hosted.grant(row) is False


def test_a_grant_after_a_taken_handle_takes_the_next(hosted_world):
    row = a_hosted(hosted_world, a_member('dup'), state=hosted.WAITING)
    hosted_world.pds.taken.add(f'dup.{hosted_world.home}')
    assert hosted.grant(row) and row.remote_name == f'dup-2.{hosted_world.home}'


def test_the_daily_task(hosted_world, monkeypatch):
    ran = []
    monkeypatch.setattr(hosted, 'daily', lambda: ran.append(1))
    hosted.daily_task.apply()
    assert ran == [1]


def test_daily_maintenance_runs_it(app, monkeypatch):
    from app import cli
    assert cli.bluesky_slots is hosted.daily and cli.bluesky_slots_task is hosted.daily_task


def test_backfill_takes_the_latest_that_may_go_and_no_more(hosted_world, monkeypatch):
    connection = a_hosted(hosted_world, hosted_world.author)
    too_old = a_microblog(hosted_world, posted_at=utcnow() - timedelta(days=4))
    oldest = a_microblog(hosted_world, posted_at=utcnow() - timedelta(days=3))
    a_microblog(hosted_world, posted_at=utcnow() - timedelta(days=2), local_only=True)   # never goes
    sent = a_microblog(hosted_world, posted_at=utcnow() - timedelta(days=1))
    newest = a_microblog(hosted_world)
    db.session.add(SocialCrosspost(post_id=sent.id, connection_id=connection.id, service='bluesky', state='sent'))
    db.session.commit()
    monkeypatch.setattr(hosted, 'BACKFILL', 2)
    assert hosted.backfill(connection) == 2
    queued = {row.post_id for row in SocialCrosspost.query.filter_by(connection_id=connection.id, state='queued')}
    assert queued == {newest.id, oldest.id} and too_old.id not in queued


def test_a_refusal_that_is_not_json(hosted_world):
    hosted_world.pds.fail['com.atproto.server.createAccount'] = httpx.Response(400, text='bad gateway page')
    with pytest.raises(Permanent):
        hosted.request_account(hosted_world.author)
    hosted_world.pds.fail['com.atproto.server.createAccount'] = httpx.Response(400, json=['odd'])
    with pytest.raises(Permanent):
        hosted.request_account(hosted_world.author)


def test_what_counts_as_a_handle_refusal(hosted_world):
    def answer(status, body):
        return httpx.Response(status, json=body) if not isinstance(body, str) else httpx.Response(status, text=body)
    assert hosted._handle_refused(answer(400, {'error': 'HandleNotAvailable'}))
    assert hosted._handle_refused(answer(400, {'error': 'InvalidRequest', 'message': 'Handle too long'}))
    assert not hosted._handle_refused(answer(400, {'error': 'InvalidRequest', 'message': 'Bad email'}))
    assert not hosted._handle_refused(answer(400, {'error': 'InvalidRequest'}))
    assert not hosted._handle_refused(answer(400, {'error': 'InvalidInviteCode'}))
    assert not hosted._handle_refused(answer(400, 'not json'))
    assert not hosted._handle_refused(answer(500, {'error': 'HandleNotAvailable'}))


def test_another_members_own_name_is_not_an_alternative(hosted_world):
    bob, bob_2 = a_member('bob'), a_member('bob_2')
    a_member('bob_3')
    with pytest.raises(hosted.HandleRefused) as refused:
        hosted.own_label(bob, 'bob-2')
    assert refused.value.suggestion == 'bob-4'   # bob-2 and bob-3 are bob_2's and bob_3's own
    assert hosted.own_label(bob_2) == 'bob-2' and hosted.next_label(bob, 'bob') == 'bob-4'
    assert hosted.own_label(bob, 'bob-4') == 'bob-4'


def test_a_name_another_member_shares_is_first_come(hosted_world):
    first, second = a_member('ann_lee'), a_member('ann-lee')
    assert hosted.own_label(first) == hosted.own_label(second) == 'ann-lee'   # the PDS gives it to one of them
    assert hosted.belongs_to_another(first, 'ann-lee')


def test_a_grant_checks_the_stored_label_again(hosted_world):
    row = a_hosted(hosted_world, a_member('waiter'), state=hosted.WAITING)
    row.sealed_tokens = seal_tokens({'label': 'admin'})
    db.session.commit()
    assert hosted.grant(row) and row.remote_name == f'waiter.{hosted_world.home}'
