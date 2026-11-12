"""gpodder sync's devices and subscriptions (plan W4b; spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md
§4, ruling G2): one list of feed URLs per person, read and written by all their devices; a URL that is a community's
own feed here joins it, and its removal leaves it."""
import base64
from datetime import timedelta

import pytest

import app.gpodder.sync as sync_module
from app import db
from app.models import CommunityBan, CommunityMember, GpodderDevice, GpodderSubscription, utcnow
from app.oauth_server import tokens
from tests.factories import make_community, make_user

OURS = 'https://test.piefed.local/community/{name}/feed'
ELSEWHERE = 'https://podcasts.example/feed.xml'


@pytest.fixture
def world(app, api_baseline):
    alice = make_user(None, 'subalice', local=True)
    bob = make_user(None, 'subbob', local=True)
    shows = make_community('shows')
    secret = make_community('secretshows')
    secret.private = True
    barred = make_community('barredshows')
    db.session.add(CommunityBan(community_id=barred.id, user_id=alice.id, banned_by=bob.id, reason='test'))
    db.session.commit()
    password = tokens.issue_app_password(alice, 'Phone')
    return {'alice': alice, 'bob': bob, 'shows': shows, 'secret': secret, 'barred': barred,
            'client': app.test_client(),
            'auth': {'Authorization': 'Basic ' + base64.b64encode(f'subalice:{password}'.encode()).decode()}}


def _upload(world, add=(), remove=(), device='phone'):
    return world['client'].post(f'/api/2/subscriptions/subalice/{device}.json', headers=world['auth'],
                                json={'add': list(add), 'remove': list(remove)})


def _pull(world, since=0, device='phone'):
    response = world['client'].get(f'/api/2/subscriptions/subalice/{device}.json?since={since}',
                                   headers=world['auth'])
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _member(world, community) -> bool:
    return CommunityMember.query.filter_by(user_id=world['alice'].id, community_id=community.id).first() is not None


class TestDevices:
    def test_a_person_has_no_devices_at_first(self, world):
        response = world['client'].get('/api/2/devices/subalice.json', headers=world['auth'])
        assert response.status_code == 200 and response.get_json() == []

    def test_a_configured_device_is_listed_with_its_subscription_count(self, world):
        response = world['client'].post('/api/2/devices/subalice/phone.json', headers=world['auth'],
                                        json={'caption': 'My phone', 'type': 'mobile'})
        assert response.status_code == 200
        _upload(world, add=[ELSEWHERE])
        listed = world['client'].get('/api/2/devices/subalice.json', headers=world['auth']).get_json()
        assert listed == [{'id': 'phone', 'caption': 'My phone', 'type': 'mobile', 'subscriptions': 1}]

    def test_configuring_again_changes_only_what_is_sent(self, world):
        world['client'].post('/api/2/devices/subalice/phone.json', headers=world['auth'],
                             json={'caption': 'My phone', 'type': 'mobile'})
        world['client'].post('/api/2/devices/subalice/phone.json', headers=world['auth'], json={'caption': 'Old'})
        device = GpodderDevice.query.filter_by(user_id=world['alice'].id).one()
        assert (device.caption, device.type) == ('Old', 'mobile')

    def test_a_device_with_no_body_is_made_with_gpodders_defaults(self, world):
        assert world['client'].post('/api/2/devices/subalice/laptop.json', headers=world['auth']).status_code == 200
        device = GpodderDevice.query.filter_by(user_id=world['alice'].id).one()
        assert (device.device_id, device.caption, device.type) == ('laptop', '', 'other')

    @pytest.mark.parametrize('path, body', [
        ('/api/2/devices/subalice/phone.json', {'type': 'toaster'}),
        ('/api/2/devices/subalice/phone.json', {'caption': 'x' * 101}),
        ('/api/2/devices/subalice/phone.json', {'caption': 5}),
        ('/api/2/devices/subalice/phone.json', ['not', 'an', 'object']),
        ('/api/2/devices/subalice/bad%20id.json', {}),
        (f"/api/2/devices/subalice/{'d' * 65}.json", {}),
    ], ids=['unknown-type', 'long-caption', 'caption-not-text', 'not-an-object', 'bad-id', 'long-id'])
    def test_what_gpodder_does_not_take_is_refused(self, world, path, body):
        response = world['client'].post(path, headers=world['auth'], json=body)
        assert response.status_code == 400 and GpodderDevice.query.count() == 0

    def test_someone_elses_devices_are_not_reached(self, world):
        assert world['client'].get('/api/2/devices/subbob.json', headers=world['auth']).status_code == 401


class TestSubscriptions:
    def test_what_is_added_is_pulled_on_any_of_the_persons_devices(self, world):
        response = _upload(world, add=[ELSEWHERE, 'https://other.example/rss'])
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        assert isinstance(body['timestamp'], int) and body['update_urls'] == []
        pulled = _pull(world, device='laptop')
        assert sorted(pulled['add']) == sorted([ELSEWHERE, 'https://other.example/rss']) and pulled['remove'] == []
        assert isinstance(pulled['timestamp'], int)
        assert {device.device_id for device in GpodderDevice.query} == {'phone', 'laptop'}

    def test_a_removal_is_pulled_as_one(self, world):
        _upload(world, add=[ELSEWHERE])
        _upload(world, remove=[ELSEWHERE])
        pulled = _pull(world)
        assert pulled['add'] == [] and pulled['remove'] == [ELSEWHERE]
        _upload(world, add=[ELSEWHERE])
        assert _pull(world)['add'] == [ELSEWHERE]

    def test_removing_what_was_never_added_changes_nothing(self, world):
        assert _upload(world, remove=[ELSEWHERE]).status_code == 200
        assert GpodderSubscription.query.count() == 0 and _pull(world) == {'add': [], 'remove': [],
                                                                            'timestamp': _pull(world)['timestamp']}

    def test_since_answers_what_changed_from_then_on(self, world):
        _upload(world, add=[ELSEWHERE])
        GpodderSubscription.query.update({'changed_at': utcnow() - timedelta(hours=1)})
        db.session.commit()
        body = _upload(world, add=['https://new.example/feed']).get_json()
        pulled = _pull(world, since=body['timestamp'])
        assert pulled['add'] == ['https://new.example/feed']

    def test_urls_are_cleaned_as_gpodder_does_and_said_so(self, world):
        body = _upload(world, add=['  https://spaced.example/feed  ', 'ftp://old.example/feed', '']).get_json()
        assert sorted(body['update_urls']) == sorted([['  https://spaced.example/feed  ', 'https://spaced.example/feed'],
                                                      ['ftp://old.example/feed', ''], ['', '']])
        assert _pull(world)['add'] == ['https://spaced.example/feed']

    @pytest.mark.parametrize('body', [{'add': [ELSEWHERE], 'remove': [ELSEWHERE]}, {'add': 'not a list'},
                                      {'add': [5]}, ['not', 'an', 'object'], {'add': ['https://x.example/' + 'y' * 2000]}],
                             ids=['both', 'not-a-list', 'not-text', 'not-an-object', 'too-long'])
    def test_an_upload_gpodder_would_refuse_is_refused(self, world, body):
        response = world['client'].post('/api/2/subscriptions/subalice/phone.json', headers=world['auth'], json=body)
        assert response.status_code == 400 and GpodderSubscription.query.count() == 0

    def test_a_person_keeps_at_most_so_many(self, world, monkeypatch):
        monkeypatch.setattr(sync_module, 'MAX_SUBSCRIPTIONS', 2)
        _upload(world, add=['https://a.example/f', 'https://b.example/f'])
        response = _upload(world, add=['https://c.example/f'])
        assert response.status_code == 400 and GpodderSubscription.query.count() == 2
        assert _upload(world, add=['https://a.example/f']).status_code == 200   # already had: no more than before

    def test_since_must_be_a_number(self, world):
        response = world['client'].get('/api/2/subscriptions/subalice/phone.json?since=yesterday',
                                        headers=world['auth'])
        assert response.status_code == 400


class TestJoining:
    def test_a_communitys_own_feed_joins_it_and_its_removal_leaves(self, world):
        url = OURS.format(name='shows')
        assert _upload(world, add=[url]).status_code == 200
        assert _member(world, world['shows'])
        assert _upload(world, remove=[url]).status_code == 200
        assert not _member(world, world['shows'])

    def test_joining_once_is_enough(self, world):
        url = OURS.format(name='shows')
        _upload(world, add=[url])
        _upload(world, add=[url])
        assert CommunityMember.query.filter_by(user_id=world['alice'].id, community_id=world['shows'].id).count() == 1

    def test_a_feed_elsewhere_or_of_no_community_joins_nothing(self, world):
        for url in ('https://podcasts.example/community/shows/feed', OURS.format(name='nosuchshows'),
                    'https://test.piefed.local/c/shows', 'https://test.piefed.local/community/shows/feed/extra'):
            assert _upload(world, add=[url]).status_code == 200
        assert not _member(world, world['shows'])
        assert GpodderSubscription.query.count() == 4

    @pytest.mark.parametrize('name', ['secretshows', 'barredshows'], ids=['private', 'banned-from'])
    def test_a_community_the_person_may_not_join_is_kept_in_the_list_alone(self, world, name):
        url = OURS.format(name=name)
        response = _upload(world, add=[url])
        assert response.status_code == 200
        assert not _member(world, world['secret'] if name == 'secretshows' else world['barred'])
        assert _pull(world)['add'] == [url]

    def test_a_moderator_who_must_step_down_first_is_left_a_member(self, world):
        url = OURS.format(name='shows')
        _upload(world, add=[url])
        CommunityMember.query.filter_by(user_id=world['alice'].id, community_id=world['shows'].id).update(
            {'is_moderator': True})
        db.session.commit()
        assert _upload(world, remove=[url]).status_code == 200
        assert _member(world, world['shows']) and _pull(world)['remove'] == [url]

    def test_a_trailing_slash_and_the_case_of_the_host_still_name_it(self, world):
        assert _upload(world, add=['https://TEST.piefed.local/community/shows/feed/']).status_code == 200
        assert _member(world, world['shows'])
