"""What this instance tells the rest of the fediverse about itself.

Sub-project 106 -- the metadata routes in `app/activitypub/routes.py`:
`.well-known/nodeinfo`, `host-meta`, both nodeinfo documents, Mastodon's
`/api/v1/instance` and `/api/v1/instance/domain_blocks`, Lemmy's
`/api/v3/site` and `/api/v3/federated_instances`, and the two
unauthenticated POST endpoints that answer whether an IP or an email address
is banned here.

Those last two are what another instance calls to share a blocklist, so they
are open to anyone and rate-limited rather than authenticated.

One defect: both of them read `request.form.get(...).split(',')`, and `.get`
answers None when the field is absent -- an `AttributeError`, which is a 500
from a POST anybody can make (D1298).

Every route here is `@cache.cached`, so each test clears the cache first:
without that the second test in a class reads the first one's body (fact 633).
"""
import pytest
from flask import current_app, g

from app import cache, db
from app.models import (AllowedInstances, BannedInstances, Instance, IpBan,
                        Site, User)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.name = 'Probeland'
    g.site.registration_mode = 'Open'
    db.session.commit()
    cache.clear()
    return SimpleNamespace(app=app, client=app.test_client(),
                           baseline=api_baseline)


class TestWhereToLookForThings:
    def test_the_nodeinfo_index_names_both_schemas(self, env):
        links = env.client.get('/.well-known/nodeinfo').get_json()['links']
        rels = {link['rel'] for link in links}
        assert 'http://nodeinfo.diaspora.software/ns/schema/2.0' in rels
        assert 'http://nodeinfo.diaspora.software/ns/schema/2.1' in rels

    def test_and_where_each_one_is(self, env):
        links = env.client.get('/.well-known/nodeinfo').get_json()['links']
        for link in links:
            assert link['href'].startswith(current_app.config['SERVER_URL'])

    def test_host_meta_points_at_webfinger(self, env):
        response = env.client.get('/.well-known/host-meta')
        assert response.status_code == 200
        assert response.content_type.startswith('application/xrd+xml')
        assert b'/.well-known/webfinger?resource={uri}' in response.data

    @pytest.mark.parametrize('path', ['/nodeinfo/2.0', '/nodeinfo/2.0.json'])
    def test_the_two_point_oh_document(self, env, path):
        cache.clear()
        document = env.client.get(path).get_json()
        assert document['version'] == '2.0'
        assert document['software']['name'] == 'piefed'

    @pytest.mark.parametrize('path', ['/nodeinfo/2.1', '/nodeinfo/2.1.json'])
    def test_the_two_point_one_document(self, env, path):
        cache.clear()
        document = env.client.get(path).get_json()
        assert document['version'] == '2.1'
        assert document['software']['name'] == 'piefed'
        assert 'repository' in document['software']


class TestWhatMastodonAsksFor:
    def test_the_instance_describes_itself(self, env):
        document = env.client.get('/api/v1/instance').get_json()
        assert document['title'] == 'Probeland'
        assert document['uri'] == current_app.config['SERVER_NAME']
        assert document['stats']['user_count'] >= 1

    def test_an_open_instance_says_registrations_are_open(self, env):
        g.site.registration_mode = 'Open'
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v1/instance').get_json()
        assert document['registrations'] is True
        assert document['approval_required'] is False

    def test_a_closed_one_says_they_are_not(self, env):
        g.site.registration_mode = 'Closed'
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v1/instance').get_json()
        assert document['registrations'] is False

    def test_one_that_reads_applications_says_so(self, env):
        g.site.registration_mode = 'RequireApplication'
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v1/instance').get_json()
        assert document['registrations'] is True
        assert document['approval_required'] is True

    def test_the_domains_this_instance_refuses(self, env):
        db.session.add(BannedInstances(domain='nasty.test', reason='spam'))
        db.session.commit()
        cache.clear()
        blocks = env.client.get('/api/v1/instance/domain_blocks').get_json()
        entry = next(block for block in blocks
                     if block['domain'] == 'nasty.test')
        assert entry['severity'] == 'suspend'
        assert entry['comment'] == 'spam'
        assert entry['digest']

    def test_a_ban_with_no_reason_given(self, env):
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        cache.clear()
        blocks = env.client.get('/api/v1/instance/domain_blocks').get_json()
        entry = next(block for block in blocks
                     if block['domain'] == 'nasty.test')
        assert entry['comment'] == ''

    def test_an_instance_on_an_allowlist_publishes_no_blocklist(self, env):
        """With an allowlist the blocklist says nothing about who is allowed,
        so publishing it would be misleading."""
        from app.utils import set_setting
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        set_setting('use_allowlist', True)
        cache.clear()
        assert env.client.get('/api/v1/instance/domain_blocks').get_json() == []


class TestWhatLemmyAsksFor:
    def test_the_site_document(self, env):
        response = env.client.get('/api/v3/site')
        assert response.status_code == 200
        assert 'site_view' in response.get_json()

    def test_the_instances_this_one_knows(self, env):
        remote = env.baseline.instance_remote
        remote.software = 'lemmy'
        remote.version = '0.19.5'
        remote.gone_forever = False
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v3/federated_instances').get_json()
        linked = document['federated_instances']['linked']
        entry = next(row for row in linked if row['domain'] == remote.domain)
        assert entry['software'] == 'lemmy'
        assert entry['version'] == '0.19.5'

    def test_an_instance_with_no_software_recorded(self, env):
        remote = env.baseline.instance_remote
        remote.software = ''
        remote.version = ''
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v3/federated_instances').get_json()
        entry = next(row for row in
                     document['federated_instances']['linked']
                     if row['domain'] == remote.domain)
        assert 'software' not in entry
        assert 'version' not in entry

    def test_this_instance_is_not_in_its_own_list(self, env):
        document = env.client.get('/api/v3/federated_instances').get_json()
        domains = {row['domain'] for row in
                   document['federated_instances']['linked']}
        assert current_app.config['SERVER_NAME'] not in domains

    def test_one_that_is_gone_forever_is_not_listed(self, env):
        remote = env.baseline.instance_remote
        remote.gone_forever = True
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v3/federated_instances').get_json()
        domains = {row['domain'] for row in
                   document['federated_instances']['linked']}
        assert remote.domain not in domains

    def test_a_blocked_instance_is_listed_as_blocked_not_linked(self, env):
        remote = env.baseline.instance_remote
        remote.gone_forever = False
        db.session.add(BannedInstances(domain=remote.domain))
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v3/federated_instances').get_json()
        federated = document['federated_instances']
        assert remote.domain in {row['domain'] for row in federated['blocked']}
        assert remote.domain not in {row['domain']
                                     for row in federated['linked']}

    def test_an_allowed_instance_is_listed_as_allowed(self, env):
        db.session.add(AllowedInstances(domain='friend.test'))
        db.session.commit()
        cache.clear()
        document = env.client.get('/api/v3/federated_instances').get_json()
        assert 'friend.test' in {row['domain'] for row in
                                 document['federated_instances']['allowed']}


class TestAskingWhetherAnAddressIsBanned:
    def test_an_ip_that_is(self, env):
        db.session.add(IpBan(ip_address='10.0.0.9'))
        db.session.commit()
        response = env.client.post('/api/is_ip_banned',
                                   data={'ip_addresses': '10.0.0.9'})
        assert response.get_json() == [True]

    def test_one_that_is_not(self, env):
        response = env.client.post('/api/is_ip_banned',
                                   data={'ip_addresses': '10.0.0.9'})
        assert response.get_json() == [False]

    def test_several_at_once_answered_in_order(self, env):
        db.session.add(IpBan(ip_address='10.0.0.9'))
        db.session.commit()
        response = env.client.post(
            '/api/is_ip_banned',
            data={'ip_addresses': '10.0.0.1,10.0.0.9,10.0.0.2'})
        assert response.get_json() == [False, True, False]

    def test_no_more_than_ten_are_answered(self, env):
        addresses = ','.join(f'10.0.0.{n}' for n in range(1, 21))
        response = env.client.post('/api/is_ip_banned',
                                   data={'ip_addresses': addresses})
        assert len(response.get_json()) == 10

    def test_a_request_that_names_no_ip_at_all(self, env):
        """D1298. `request.form.get('ip_addresses')` is None when the field is
        absent, and `None.split(',')` was an AttributeError -- a 500 from an
        unauthenticated POST."""
        response = env.client.post('/api/is_ip_banned', data={})
        assert response.status_code == 200
        assert response.get_json() == [False]

    def test_an_email_that_belongs_to_a_banned_account(self, env):
        banned = make_user(env.baseline.instance_local, 'troublemaker',
                           local=True)
        banned.email = 'trouble@example.test'
        banned.banned = True
        db.session.commit()
        response = env.client.post('/api/is_email_banned',
                                   data={'emails': 'trouble@example.test'})
        assert response.get_json() == [True]

    def test_one_that_belongs_to_an_account_in_good_standing(self, env):
        response = env.client.post(
            '/api/is_email_banned',
            data={'emails': env.baseline.user2.email})
        assert response.get_json() == [False]

    def test_one_nobody_here_has(self, env):
        response = env.client.post('/api/is_email_banned',
                                   data={'emails': 'nobody@example.test'})
        assert response.get_json() == [False]

    def test_a_banned_account_on_another_instance_does_not_count(self, env):
        """The question is about accounts THIS instance banned."""
        remote = make_user(env.baseline.instance_remote, 'faraway')
        remote.ap_id = 'faraway@remote.test'
        remote.email = 'faraway@example.test'
        remote.banned = True
        db.session.commit()
        response = env.client.post('/api/is_email_banned',
                                   data={'emails': 'faraway@example.test'})
        assert response.get_json() == [False]

    def test_space_around_an_address_is_ignored(self, env):
        banned = make_user(env.baseline.instance_local, 'troublemaker',
                           local=True)
        banned.email = 'trouble@example.test'
        banned.banned = True
        db.session.commit()
        response = env.client.post('/api/is_email_banned',
                                   data={'emails': ' trouble@example.test '})
        assert response.get_json() == [True]

    def test_no_more_than_ten_addresses_are_answered(self, env):
        emails = ','.join(f'person{n}@example.test' for n in range(1, 21))
        response = env.client.post('/api/is_email_banned',
                                   data={'emails': emails})
        assert len(response.get_json()) == 10

    def test_a_request_that_names_no_email_at_all(self, env):
        """D1298, the second site. This and the IP one above had the SAME
        method name until the mutation pass showed one of them was never
        running: the second definition in a class silently replaces the
        first."""
        response = env.client.post('/api/is_email_banned', data={})
        assert response.status_code == 200
        assert response.get_json() == [False]
