"""Subscribing to relays (spec: Subscribing)."""
import pytest

from app import db
from app.models import Relay
from app.relays import (PUBLIC, RELAY_ACCEPTED, RELAY_FAILED, RELAY_PENDING, RELAY_REFUSED, STYLE_LITEPUB,
                        STYLE_MASTODON)
from app.relays import subscribe
from app.relays.refusals import record_relay_refusal

pytestmark = pytest.mark.usefixtures('site')

ACTOR = {'id': 'https://relay.example/actor', 'type': 'Application', 'inbox': 'https://relay.example/inbox',
         'publicKey': {'id': 'https://relay.example/actor#main-key', 'publicKeyPem': 'PEM'}}
TAG = {'id': 'https://relay.fedi.buzz/tag/cats', 'type': 'Service', 'inbox': 'https://relay.fedi.buzz/tag/cats/inbox',
       'publicKey': {'id': 'https://relay.fedi.buzz/tag/cats#key', 'publicKeyPem': 'TAGPEM'}}


@pytest.fixture
def net(monkeypatch):
    calls = {'get': [], 'post': []}
    documents = {}

    def fake_get(uri):
        calls['get'].append(uri)
        return documents.get(uri)

    def fake_post(uri, body, private_key, key_id, **kwargs):
        calls['post'].append((uri, body, key_id))
        return True

    monkeypatch.setattr(subscribe, 'remote_object_to_json', fake_get)
    monkeypatch.setattr(subscribe, 'send_post_request', fake_post)
    calls['documents'] = documents
    return calls


class TestDetect:

    def test_an_inbox_url_is_mastodon_style_and_its_actor_is_looked_up(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.example/inbox')
        assert found == {'style': STYLE_MASTODON, 'inbox_url': 'https://relay.example/inbox',
                         'actor_id': 'https://relay.example/actor', 'public_key': 'PEM'}

    def test_a_mastodon_relay_whose_actor_cannot_be_fetched_has_no_key(self, app, net):
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.example/inbox')
        assert found['actor_id'] is None and found['public_key'] is None

    def test_a_fedibuzz_tag_url_is_litepub_style(self, app, net):
        net['documents']['https://relay.fedi.buzz/tag/cats'] = TAG
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.fedi.buzz/tag/cats')
        assert found == {'style': STYLE_LITEPUB, 'inbox_url': 'https://relay.fedi.buzz/tag/cats/inbox',
                         'actor_id': 'https://relay.fedi.buzz/tag/cats', 'public_key': 'TAGPEM'}

    @pytest.mark.parametrize('document', [None, {'id': 'https://x.example/a'}])
    def test_a_litepub_url_without_a_usable_actor_is_refused(self, app, net, document):
        net['documents']['https://x.example/a'] = document
        with app.test_request_context(), pytest.raises(subscribe.RelayError):
            subscribe.detect_relay('https://x.example/a')


    @pytest.mark.parametrize('kind', ['Person', 'Group', None])
    def test_a_litepub_url_whose_actor_is_not_a_relay_type_is_refused(self, app, net, kind):
        net['documents']['https://x.example/a'] = {**ACTOR, 'id': 'https://x.example/a', 'type': kind}
        with app.test_request_context(), pytest.raises(subscribe.RelayError, match='is not a relay actor'):
            subscribe.detect_relay('https://x.example/a')

    def test_a_mastodon_actor_that_is_not_a_relay_type_is_treated_as_unknown(self, app, net):
        net['documents']['https://relay.example/actor'] = {**ACTOR, 'type': 'Person'}
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.example/inbox')
        assert found['actor_id'] is None and found['public_key'] is None and found['style'] == STYLE_MASTODON


class TestFollow:

    def test_add_stores_a_pending_row_and_follows_public_for_mastodon(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
        uri, body, key_id = net['post'][-1]
        assert relay.state == RELAY_PENDING
        assert uri == 'https://relay.example/inbox'
        assert body['type'] == 'Follow' and body['object'] == PUBLIC
        # Activity-Relay (yukimochi) reads a Follow addressed to Public as an activity to relay, and drops it
        # with a 202: the Follow is addressed to nobody, as Mastodon's own relay Follow is.
        assert 'to' not in body and 'cc' not in body
        assert body['actor'].endswith('/actor') and key_id.endswith('/actor#main-key')
        assert body['id'] == relay.follow_activity_id and '/activities/relay-follow/' in body['id']

    def test_a_litepub_follow_names_the_relay_actor(self, app, net):
        net['documents']['https://relay.fedi.buzz/tag/cats'] = TAG
        with app.test_request_context():
            subscribe.add_relay('https://relay.fedi.buzz/tag/cats')
        body = net['post'][-1][1]
        assert body['object'] == 'https://relay.fedi.buzz/tag/cats' and body['to'] == ['https://relay.fedi.buzz/tag/cats']

    def test_adding_the_same_url_twice_is_refused(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            subscribe.add_relay('https://relay.example/inbox')
            with pytest.raises(subscribe.RelayError):
                subscribe.add_relay('https://relay.example/inbox')

    def test_retry_sends_a_new_follow_id_and_goes_back_to_pending(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            first = relay.follow_activity_id
            relay.state = RELAY_REFUSED
            db.session.commit()
            subscribe.retry_relay(relay)
        assert relay.state == RELAY_PENDING and relay.follow_activity_id != first

    def test_retry_keeps_a_known_actor_and_key_when_the_actor_cannot_be_fetched(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            first = relay.follow_activity_id
            del net['documents']['https://relay.example/actor']
            subscribe.retry_relay(relay)
        assert relay.actor_id == 'https://relay.example/actor' and relay.public_key == 'PEM'
        assert relay.follow_activity_id != first and net['post'][-1][1]['id'] == relay.follow_activity_id

    def test_retry_replaces_a_known_key_with_a_fetched_one(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            net['documents']['https://relay.example/actor'] = {**ACTOR, 'publicKey': {'publicKeyPem': 'NEW'}}
            subscribe.retry_relay(relay)
        assert relay.public_key == 'NEW'


FOLLOWERS = 'https://relay.example/followers'


class TestRetryFindsUsAmongTheFollowers:
    """barkshark ActivityRelay lists accepted instances in its followers collection; an Accept it sent may
    never have arrived, so Retry looks there before following again."""

    def _listed(self, app, net, collection, actor=None):
        net['documents']['https://relay.example/actor'] = actor or {**ACTOR, 'followers': FOLLOWERS}
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            first, posts = relay.follow_activity_id, len(net['post'])
            net['documents'][FOLLOWERS] = collection(subscribe.instance_actor_url())
            subscribe.retry_relay(relay)
        return relay, first, net['post'][posts:]

    @pytest.mark.parametrize('collection', [
        lambda me: {'type': 'Collection', 'items': ['https://other.example/actor', me]},
        lambda me: {'type': 'OrderedCollection', 'orderedItems': [{'id': me, 'type': 'Application'}]},
        lambda me: {'type': 'OrderedCollection', 'first': {'type': 'OrderedCollectionPage', 'orderedItems': [me]}},
    ])
    def test_a_relay_listing_the_instance_actor_is_accepted_without_a_new_follow(self, app, net, collection):
        relay, first, posts = self._listed(app, net, collection)
        assert relay.state == RELAY_ACCEPTED and relay.answered_at is not None and relay.last_error is None
        assert relay.follow_activity_id == first and posts == []

    def test_a_first_page_given_as_a_url_is_fetched(self, app, net):
        with app.test_request_context():
            net['documents'][FOLLOWERS + '?page=1'] = {'orderedItems': [subscribe.instance_actor_url()]}
        relay, _, _ = self._listed(app, net, lambda me: {'first': FOLLOWERS + '?page=1'})
        assert relay.state == RELAY_ACCEPTED

    @pytest.mark.parametrize('collection', [
        lambda me: {'type': 'Collection', 'items': ['https://other.example/actor']},
        lambda me: None,
        lambda me: {'type': 'Collection', 'items': 'not a list'},
    ])
    def test_a_relay_not_listing_us_is_followed_again(self, app, net, collection):
        relay, first, posts = self._listed(app, net, collection)
        assert relay.state == RELAY_PENDING and relay.follow_activity_id != first
        assert [body['type'] for _, body, _ in posts] == ['Follow']

    def test_a_relay_actor_with_no_followers_collection_is_followed_again(self, app, net):
        relay, first, posts = self._listed(app, net, lambda me: {'items': [me]}, actor=ACTOR)
        assert relay.state == RELAY_PENDING and [body['type'] for _, body, _ in posts] == ['Follow']

    def test_a_followers_collection_on_another_host_is_not_fetched(self, app, net):
        elsewhere = 'https://elsewhere.example/followers'
        with app.test_request_context():
            net['documents'][elsewhere] = {'items': [subscribe.instance_actor_url()]}
        relay, _, posts = self._listed(app, net, lambda me: None, actor={**ACTOR, 'followers': elsewhere})
        assert relay.state == RELAY_PENDING and elsewhere not in net['get']

    def test_remove_sends_undo_and_deletes(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            follow = subscribe.relay_follow_activity(relay)
            subscribe.remove_relay(relay)
        undo = net['post'][-1][1]
        assert undo['type'] == 'Undo' and undo['object'] == follow
        assert Relay.query.count() == 0

    def test_remove_still_deletes_when_the_undo_cannot_be_sent(self, app, net, monkeypatch):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')

            def broken(*args, **kwargs):
                raise OSError('down')
            monkeypatch.setattr(subscribe, 'send_post_request', broken)
            subscribe.remove_relay(relay)
        assert Relay.query.count() == 0


class TestRefusal:

    @pytest.mark.parametrize('status, expected', [(403, RELAY_FAILED), (500, RELAY_PENDING)])
    def test_a_definitive_refusal_marks_the_row_failed(self, app, net, status, expected):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            record_relay_refusal(db.session, subscribe.relay_follow_activity(relay), status)
        assert relay.state == expected

    def test_an_accepted_relay_is_not_failed_by_a_late_refusal(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            relay.state = RELAY_ACCEPTED
            db.session.commit()
            record_relay_refusal(db.session, subscribe.relay_follow_activity(relay), 403)
        assert relay.state == RELAY_ACCEPTED

    @pytest.mark.parametrize('body', [None, {'type': 'Create'}, {'type': 'Follow', 'id': 'https://elsewhere/x'}])
    def test_other_bodies_are_ignored(self, app, body):
        with app.test_request_context():
            record_relay_refusal(db.session, body, 403)
