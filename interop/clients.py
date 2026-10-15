"""Thin API clients for the harness apps. Every wait is bounded: a slow interop test is a bug (spec §8.1)."""
import json
import os
import ssl
import time

import httpx

CA = '/tmp/harness-ca.crt'


def wait_for(fn, timeout=30.0, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f'wait_for: {getattr(fn, "__name__", fn)} never became true in {timeout}s')
        time.sleep(interval)


def tokens() -> dict:
    with open(os.path.join(os.path.dirname(__file__), '.tokens.json')) as f:
        return json.load(f)


class Mastodon:
    def __init__(self, token):
        self.http = httpx.Client(base_url='https://mastodon.test', verify=ssl.create_default_context(cafile=CA), timeout=10,
                                 headers={'Authorization': f'Bearer {token}'})

    def resolve_account(self, acct):
        r = self.http.get('/api/v2/search', params={'q': acct, 'resolve': 'true', 'type': 'accounts'})
        r.raise_for_status()
        accounts = r.json()['accounts']
        return accounts[0] if accounts else None

    def follow(self, account_id):
        return self.http.post(f'/api/v1/accounts/{account_id}/follow').raise_for_status().json()

    def relationship(self, account_id):
        return self.http.get('/api/v1/accounts/relationships', params={'id[]': account_id}).raise_for_status().json()[0]

    def home(self):
        return self.http.get('/api/v1/timelines/home', params={'limit': 40}).raise_for_status().json()

    def search_status(self, url):
        r = self.http.get('/api/v2/search', params={'q': url, 'resolve': 'true', 'type': 'statuses'})
        r.raise_for_status()
        statuses = r.json()['statuses']
        return statuses[0] if statuses else None

    def status(self, status_id):
        r = self.http.get(f'/api/v1/statuses/{status_id}')
        return None if r.status_code == 404 else r.raise_for_status().json()

    def reply(self, status_id, text):
        return self.http.post('/api/v1/statuses', data={'status': text, 'in_reply_to_id': status_id}).raise_for_status().json()

    def favourite(self, status_id):
        self.http.post(f'/api/v1/statuses/{status_id}/favourite').raise_for_status()

    def reblog(self, status_id):
        self.http.post(f'/api/v1/statuses/{status_id}/reblog').raise_for_status()


class PieFed:
    """Alpha API routes and payload names checked against app/api/alpha/routes.py and schema.py."""

    def __init__(self, user, password):
        self.http = httpx.Client(base_url='https://piefed.test/api/alpha', verify=ssl.create_default_context(cafile=CA), timeout=10)
        jwt = self.http.post('/user/login', json={'username': user, 'password': password}).raise_for_status().json()['jwt']
        self.http.headers['Authorization'] = f'Bearer {jwt}'

    def microblogs_id(self):
        return self.http.get('/community', params={'name': 'microblogs'}).raise_for_status().json()['community_view']['community']['id']

    def post(self, title, body, community_id, **extra):
        payload = {'title': title, 'body': body, 'community_id': community_id, **extra}
        return self.http.post('/post', json=payload).raise_for_status().json()['post_view']

    def edit(self, post_id, **fields):
        return self.http.put('/post', json={'post_id': post_id, **fields}).raise_for_status().json()['post_view']

    def delete(self, post_id):
        self.http.post('/post/delete', json={'post_id': post_id, 'deleted': True}).raise_for_status()

    def get_post(self, post_id):
        return self.http.get('/post', params={'id': post_id}).raise_for_status().json()['post_view']

    def replies(self, post_id):
        return self.http.get('/comment/list', params={'post_id': post_id}).raise_for_status().json()['comments']


class PeerTube:
    """REST API v1 routes as PeerTube v8.3 documents them (https://docs.joinpeertube.org/api-rest-reference.html)."""

    def __init__(self, token):
        self.http = httpx.Client(base_url='https://peertube.test/api/v1', verify=ssl.create_default_context(cafile=CA),
                                 timeout=10, headers={'Authorization': f'Bearer {token}'})

    def me(self):
        return self.http.get('/users/me').raise_for_status().json()

    def subscribe(self, handle):
        """Subscribe to a channel by handle (`name@host`); PeerTube resolves it by webfinger and sends Follow."""
        self.http.post('/users/me/subscriptions', json={'uri': handle}).raise_for_status()

    def subscription(self, handle):
        r = self.http.get(f'/users/me/subscriptions/{handle}')
        return None if r.status_code == 404 else r.raise_for_status().json()

    def channel_videos(self, handle):
        return self.http.get(f'/video-channels/{handle}/videos').raise_for_status().json()['data']

    def video(self, video_id):
        r = self.http.get(f'/videos/{video_id}')
        return None if r.status_code == 404 else r.raise_for_status().json()
