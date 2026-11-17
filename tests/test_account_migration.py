"""Account migration (Mastodon's Move).

Moving an account HERE: Mastodon moves an account only to one whose actor names the old account in `alsoKnownAs`
(a back-reference). A local user adds the old account as an alias at /user/settings/aliases, and the actor document
then lists it.

A Move received: `{type: Move, actor: old, object: old, target: new}`. It reached Lemmy's post-Move branch and
raised KeyError('origin'). Now each local user who followed the old account follows the new one instead -- only
when the account itself sent it and the target names it in alsoKnownAs.
"""
from unittest.mock import patch

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, User, UserFollower, utcnow
from tests.factories import inbox_activity, make_user
from tests.test_ap_interop_actor_update import _known_remote_user
from tests.test_ap_refresh_profiles import PEER
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """The app's login_required checks a POST's CSRF token itself, even with WTF_CSRF_ENABLED off."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def accounts(app, db_session):
    """`old`, a remote account on PEER; `new` and `fan`, local accounts. `fan` follows `old`."""
    old = _known_remote_user('wakko')
    new = make_user(None, 'wakko_here', local=True, with_keys=True)
    fan = make_user(None, 'fan', local=True, with_keys=True)
    for local in (new, fan):   # registration stores it; the factory does not, and local lookup by URL reads it
        local.ap_profile_id = local.public_url().lower()
    db.session.add(UserFollower(local_user_id=fan.id, remote_user_id=old.id, is_accepted=True, is_inward=False))
    fan.num_following = 1
    old.num_followers = 1
    db.session.commit()
    return old, new, fan


def follows(follower, followed):
    return db.session.query(UserFollower).filter_by(local_user_id=follower.id, remote_user_id=followed.id,
                                                    is_inward=False).first()


def move(old, target_url, **fields):
    return inbox_activity(old, activity_type='Move', object_uri=old.ap_profile_id, target=target_url, **fields)


# --- the actor document ---

class TestTheActorDocument:
    def test_an_alias_is_published_as_also_known_as(self, app, accounts):
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        db.session.commit()

        response = app.test_client().get(f'/u/{new.user_name}', headers={'Accept': 'application/activity+json'})

        document = response.get_json()
        assert document['alsoKnownAs'] == [old.ap_profile_id]
        assert {'alsoKnownAs': {'@id': 'as:alsoKnownAs', '@type': '@id'}} in document['@context']

    def test_an_account_without_aliases_has_no_also_known_as(self, app, accounts):
        old, new, fan = accounts

        response = app.test_client().get(f'/u/{new.user_name}', headers={'Accept': 'application/activity+json'})

        assert 'alsoKnownAs' not in response.get_json()


# --- the settings page ---

class TestTheAliasesPage:
    def add(self, app, client, handle, found):
        with patch('app.user.routes.find_actor_or_create', return_value=found) as lookup:
            response = client.post('/user/settings/aliases', data={'handle': handle, 'csrf_token': csrf(app, client)})
        return response, lookup

    def test_an_old_account_is_added_by_its_handle(self, app, accounts):
        old, new, fan = accounts
        client = app.test_client()
        login(client, new)

        response, lookup = self.add(app, client, '@wakko@peer.example', old)

        assert response.status_code == 302
        lookup.assert_called_once_with('wakko@peer.example')
        assert db.session.get(User, new.id).also_known_as == [old.ap_profile_id]

    @pytest.mark.parametrize('found', ['nobody', 'local'])
    def test_an_unknown_or_local_account_is_refused(self, app, accounts, found):
        old, new, fan = accounts
        client = app.test_client()
        login(client, new)

        response, lookup = self.add(app, client, 'someone@peer.example', None if found == 'nobody' else fan)

        assert response.status_code == 200
        message = 'No account was found' if found == 'nobody' else 'That is an account on this server'
        assert message in response.get_data(as_text=True)
        assert not db.session.get(User, new.id).also_known_as

    @pytest.mark.parametrize('handle', ['wakko@peer.example', 'https://peer.example/@wakko'])
    def test_an_account_on_a_blocked_server_says_so(self, app, accounts, handle):
        """validate_remote_actor refuses a banned server's actors silently, so the page said "no account was found"
        for an account that exists. It names the block now, and does not look the account up."""
        from app.models import BannedInstances
        old, new, fan = accounts
        db.session.add(BannedInstances(domain='peer.example'))
        db.session.commit()
        client = app.test_client()
        login(client, new)

        response, lookup = self.add(app, client, handle, old)

        html = response.get_data(as_text=True)
        assert 'is blocked by this server' in html and 'No account was found' not in html
        lookup.assert_not_called()
        assert not db.session.get(User, new.id).also_known_as

    def test_an_account_on_a_server_off_the_allowlist_says_so(self, app, accounts):
        from app.utils import set_setting
        old, new, fan = accounts
        set_setting('use_allowlist', True)
        client = app.test_client()
        login(client, new)

        response, lookup = self.add(app, client, 'wakko@peer.example', old)

        assert "is not on this server&#39;s allowlist" in response.get_data(as_text=True)
        lookup.assert_not_called()

    def test_the_page_lists_and_removes_an_alias(self, app, accounts):
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        db.session.commit()
        client = app.test_client()
        login(client, new)

        token = csrf(app, client)   # first: rendering the page caches a token on g, which generate_csrf then reuses
        html = client.get('/user/settings/aliases').get_data(as_text=True)
        assert old.ap_profile_id in html and new.mention_tag() in html

        client.post('/user/settings/aliases/remove', data={'alias': old.ap_profile_id, 'csrf_token': token})

        assert db.session.get(User, new.id).also_known_as == []


# --- a Move received ---

class TestAMoveReceived:
    @pytest.fixture(autouse=True)
    def logged(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        # the old account is remote: its Undo goes out through this; nothing is sent from a test
        self.sent = []
        monkeypatch.setattr('app.shared.user.task_selector', lambda *args, **kwargs: self.sent.append((args, kwargs)))

    def test_a_follower_of_the_old_account_follows_the_new_one(self, app, accounts):
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        db.session.commit()
        ids = old.id, new.id, fan.id

        dispatch(move(old, new.public_url()))

        db.session.expire_all()
        old, new, fan = (db.session.get(User, i) for i in ids)
        assert follows(fan, new).is_accepted is True
        assert follows(fan, old) is None
        assert fan.num_following == 1 and new.num_followers == 1 and old.num_followers == 0
        assert (('unfollow_user',), {'to_follow_id': old.id, 'user_id': fan.id}) in self.sent
        log = ActivityPubLog.query.one()
        assert log.result == 'success' and '1 local follows moved' in log.exception_message

    def test_a_target_that_does_not_name_the_old_account_is_refused(self, app, accounts):
        """Without the back-reference any account could claim another's followers."""
        old, new, fan = accounts

        dispatch(move(old, new.public_url()))

        assert follows(fan, old) is not None and follows(fan, new) is None
        log = ActivityPubLog.query.one()
        assert log.result == 'failure' and 'alsoKnownAs' in log.exception_message

    def test_a_move_of_another_account_is_refused(self, app, accounts):
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        db.session.commit()
        activity = move(old, new.public_url())
        activity['object'] = f'https://{PEER}/u/someone_else'

        dispatch(activity)

        assert follows(fan, old) is not None and follows(fan, new) is None
        assert ActivityPubLog.query.one().exception_message == 'Move of another account refused'

    @pytest.mark.parametrize('block', ['follower_blocks_new', 'new_blocks_follower', 'follower_blocks_server'])
    def test_a_block_is_never_overridden(self, app, accounts, block):
        """As Mastodon's own move: the follower is left as they were, still following the old account."""
        from app.models import InstanceBlock, UserBlock
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        if block == 'follower_blocks_new':
            db.session.add(UserBlock(blocker_id=fan.id, blocked_id=new.id))
        elif block == 'new_blocks_follower':
            db.session.add(UserBlock(blocker_id=new.id, blocked_id=fan.id))
        else:
            db.session.add(InstanceBlock(user_id=fan.id, instance_id=new.instance_id))
        db.session.commit()

        dispatch(move(old, new.public_url()))

        assert follows(fan, new) is None and follows(fan, old) is not None
        assert '0 local follows moved' in ActivityPubLog.query.one().exception_message

    def test_a_banned_target_moves_nobody(self, app, accounts):
        from app.shared.user import move_account_followers
        old, new, fan = accounts
        new.banned = True
        db.session.commit()

        assert move_account_followers(old, new) == 0
        assert follows(fan, new) is None and follows(fan, old) is not None

    def test_a_remote_target_is_checked_against_its_fetched_document(self, app, accounts, monkeypatch):
        old, new, fan = accounts
        elsewhere = make_user(old.instance, 'wakko2')
        elsewhere.ap_profile_id = elsewhere.ap_public_url = f'https://{PEER}/u/wakko2'
        elsewhere.ap_fetched_at = utcnow()
        db.session.commit()
        ids = old.id, elsewhere.id, fan.id
        monkeypatch.setattr(activitypub_routes, 'remote_object_to_json',
                            lambda uri, retry_delay=3: {'id': uri, 'alsoKnownAs': old.ap_profile_id.upper()})

        dispatch(move(old, elsewhere.ap_profile_id))

        db.session.expire_all()
        old, elsewhere, fan = (db.session.get(User, i) for i in ids)
        assert follows(fan, elsewhere) is not None and follows(fan, old) is None
        assert (('follow_user',), {'to_follow_id': elsewhere.id, 'user_id': fan.id}) in self.sent

    def test_a_lemmy_post_move_still_takes_its_own_branch(self, app, accounts):
        """An `origin` marks Lemmy's post Move: not read as an account Move."""
        old, new, fan = accounts
        new.also_known_as = [old.ap_profile_id]
        db.session.commit()

        with patch.object(activitypub_routes, 'process_account_move') as account_move, \
                patch.object(activitypub_routes, 'find_actor_or_create_cached', return_value=None):
            dispatch(move(old, new.public_url(), origin=f'https://{PEER}/c/a'))

        account_move.assert_not_called()
