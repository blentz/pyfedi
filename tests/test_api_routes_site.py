"""The alpha API over HTTP: the site, search, feeds, topics and the
registration queue.

Sub-project 85, slice B -- fifteen routes of `app/api/alpha/routes.py`, driven
through the test client with the API switched on, so that each route's body
runs: the Authorization header is read, the utils function is called, and the
RESPONSE IS VALIDATED AGAINST ITS OWN SCHEMA. That last step is what these
tests are for. The utils functions already have their own coverage; what no
test reached until now is whether what they return is what the schema the
route declares says they return -- and the first thing this slice found was a
route whose schema rejected its own answer (D1250).
"""
import pytest
from flask import current_app, g

from app import db
from app.models import Site, UserRegistration, utcnow
from app.utils import set_setting
from tests.factories import make_community, make_community_member, make_post


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    """The API switched on, and a client to drive it with."""
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    return SimpleNamespace(client=app.test_client(), app=app,
                           site=g.site,
                           reader=api_baseline.user3,
                           admin=api_baseline.user1,
                           baseline=api_baseline)


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


class TestTheSite:
    def test_the_site(self, env):
        response = env.client.get('/api/alpha/site')
        assert response.status_code == 200
        assert 'site' in response.get_json()

    def test_the_site_as_an_account(self, env):
        response = env.client.get('/api/alpha/site',
                                  headers=auth(env.reader))
        assert response.status_code == 200
        assert response.get_json()['my_user']['local_user_view'][
            'person']['id'] == env.reader.id

    def test_the_version(self, env):
        response = env.client.get('/api/alpha/site/version')
        assert response.status_code == 200
        assert 'version' in response.get_json()

    def test_the_federated_instances(self, env):
        response = env.client.get('/api/alpha/federated_instances')
        assert response.status_code == 200
        assert 'federated_instances' in response.get_json()

    def test_blocking_an_instance(self, env):
        remote = env.baseline.instance_remote
        response = env.client.post('/api/alpha/site/block',
                                   headers=auth(env.reader),
                                   json={'instance_id': remote.id,
                                         'block': True})
        assert response.status_code == 200
        assert response.get_json() == {'blocked': True}

    def test_blocking_an_instance_nobody_holds(self, env):
        response = env.client.post('/api/alpha/site/block',
                                   headers=auth(env.reader),
                                   json={'instance_id': 999999,
                                         'block': True})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'instance not found'

    def test_blocking_without_an_account(self, env):
        remote = env.baseline.instance_remote
        response = env.client.post('/api/alpha/site/block',
                                   json={'instance_id': remote.id,
                                         'block': True})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'incorrect_login'


class TestTheInstanceChooser:
    def test_the_chooser(self, env):
        set_setting('enable_instance_chooser', True)
        response = env.client.get('/api/alpha/site/instance_chooser')
        assert response.status_code == 200
        assert 'elevator_pitch' in response.get_json()

    def test_the_chooser_when_this_instance_has_no_language(self, env):
        """D1250. The schema declared `language` required and not nullable, so
        the endpoint answered 400 for an instance that had never chosen one --
        which is the only kind the chooser exists to introduce."""
        set_setting('enable_instance_chooser', True)
        env.site.language_id = None
        db.session.commit()
        response = env.client.get('/api/alpha/site/instance_chooser')
        assert response.status_code == 200
        assert response.get_json()['language'] is None

    def test_the_chooser_search(self, env):
        set_setting('enable_instance_chooser', True)
        response = env.client.get('/api/alpha/site/instance_chooser_search')
        assert response.status_code == 200
        assert response.get_json() == {'result': []}


class TestSearch:
    def test_a_search(self, env):
        response = env.client.get('/api/alpha/search',
                                  query_string={'q': 'nothing',
                                                'type_': 'Posts'})
        assert response.status_code == 200
        assert response.get_json()['type_'] == 'Posts'

    def test_a_search_needs_something_to_search_for(self, env):
        response = env.client.get('/api/alpha/search')
        assert response.status_code == 400

    def test_resolving_something_this_instance_does_not_know(self, env):
        response = env.client.get('/api/alpha/resolve_object',
                                  headers=auth(env.reader),
                                  query_string={'q': 'https://far.test/p/1'})
        assert response.status_code == 400

    def test_suggesting_completions(self, env):
        response = env.client.get('/api/alpha/suggest_completion',
                                  headers=auth(env.reader),
                                  query_string={'q': 'prob'})
        assert response.status_code == 200
        assert response.get_json() == {'result': []}

    def test_the_modlog(self, env):
        response = env.client.get('/api/alpha/modlog')
        assert response.status_code == 200
        assert response.get_json()['banned'] == []


class TestFeedsAndTopics:
    def test_the_feed_listing(self, env):
        response = env.client.get('/api/alpha/feed/list')
        assert response.status_code == 200
        assert 'feeds' in response.get_json()

    def test_one_feed(self, env):
        # `user_id` and `ap_domain` as the product sets them
        # (app/shared/feed.py): the factory leaves both null, and FeedView
        # declares them non-nullable.
        from tests.factories import make_local_feed
        feed = make_local_feed('newsfeed', public=True)
        feed.user_id = env.admin.id
        feed.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        response = env.client.get('/api/alpha/feed',
                                  query_string={'id': feed.id})
        assert response.status_code == 200
        assert response.get_json()['id'] == feed.id

    def test_a_feed_nobody_holds(self, env):
        response = env.client.get('/api/alpha/feed',
                                  query_string={'id': 999999})
        assert response.status_code == 400

    def test_the_topic_listing(self, env):
        response = env.client.get('/api/alpha/topic/list')
        assert response.status_code == 200
        assert 'topics' in response.get_json()


class TestTheRegistrationQueue:
    @pytest.fixture
    def applicant(self, env):
        from tests.factories import make_user, make_user_registration
        who = make_user(env.baseline.instance_local, 'hopeful', local=True)
        who.verified = True
        who.ip_address = '203.0.113.4'
        make_user_registration(who, answer='let me in')
        db.session.commit()
        return who

    @pytest.fixture
    def applicant_with_no_address(self, env):
        from tests.factories import make_user, make_user_registration
        who = make_user(env.baseline.instance_local, 'anonymous', local=True)
        who.verified = True
        who.ip_address = None
        make_user_registration(who, answer='let me in')
        db.session.commit()
        return who

    def test_an_applicant_whose_address_was_never_recorded(
            self, env, applicant_with_no_address):
        """D1251. The schema declares `ip_address` required AND nullable, and
        the view omitted the key rather than sending null -- so ONE such
        applicant made the whole queue a 400, and the admin could not see any
        of it."""
        response = env.client.get(
            '/api/alpha/admin/registration_application/list',
            headers=auth(env.admin),
            query_string={'unread_only': True})
        assert response.status_code == 200
        listed = response.get_json()['registrations']
        assert listed[0]['ip_address'] is None

    def test_an_administrator_reads_the_queue(self, env, applicant):
        response = env.client.get(
            '/api/alpha/admin/registration_application/list',
            headers=auth(env.admin),
            query_string={'unread_only': True})
        assert response.status_code == 200
        assert len(response.get_json()['registrations']) == 1

    def test_an_account_with_no_standing_cannot(self, env, applicant):
        response = env.client.get(
            '/api/alpha/admin/registration_application/list',
            headers=auth(env.reader),
            query_string={'unread_only': True})
        assert response.status_code == 400

    def test_an_administrator_approves_an_application(self, env, applicant):
        # The request names the APPLICANT, not the application
        # (RegistrationApproveRequest.user_id).
        response = env.client.put(
            '/api/alpha/admin/registration_application/approve',
            headers=auth(env.admin),
            json={'user_id': applicant.id, 'approve': True})
        assert response.status_code == 200
        assert UserRegistration.query.filter_by(
            user_id=applicant.id).one().status == 1

    def test_an_account_with_no_standing_cannot_approve(self, env, applicant):
        response = env.client.put(
            '/api/alpha/admin/registration_application/approve',
            headers=auth(env.reader),
            json={'user_id': applicant.id, 'approve': True})
        assert response.status_code == 400
        assert UserRegistration.query.filter_by(
            user_id=applicant.id).one().status != 1



