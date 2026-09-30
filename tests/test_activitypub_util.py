"""find_actor_or_create, its cached wrapper, and HttpSignature.signed_request.

This file used to build its own Flask app from its own TestConfig, which
bypassed tests/conftest.py entirely: no db_session (so no truncation and no
isolation), no redis_double, and -- the reason it was excluded from every run --
no block_outbound_http. It also assumed a seeded developer database containing a
user literally named `rimuadmin`, and it fetched a real community from the real
piefed.social over the real internet. It now takes `app`, `db_session`, `site`
and `http_mock` from tests/conftest.py like every other file here, builds its
own rows with tests/factories.py, and serves the one remote actor it needs from
respx.

Two things a later reader will otherwise rediscover the hard way:

**A local user is not reachable by its own `name@server` handle.**
`find_actor_by_url`'s webfinger-shaped branch matches `User.ap_id`, and a local
user's `ap_id` is NULL by definition (`User.is_local()` is exactly
`ap_id is None or ...`). So `find_actor_or_create('someone@my.own.server')`
never finds the local `someone`; it falls through to `create_if_not_found`,
which webfingers the instance's own domain. The original test asserted that call
returned an actor, and it could only do so by going to the network and back.
That assertion is preserved here against a REMOTE handle -- the same dispatch
branch, the same claim about `name@domain` actor strings -- and the local form is
recorded as the defect it is rather than dropped silently.

**`find_actor_or_create_cached`'s write-back key is dead.** The memoized inner
helper is keyed by Flask-Caching's own memoize scheme (a function namespace plus
a hash of the arguments, e.g. `3jJZ3uF5IV9/Aa1kvN+3fv`), while the wrapper's
fallback path writes its "cache the actor for next time" entry under the
hand-built string `_find_actor_id_cached(<url>,<community_only>,<feed_only>)`.
Nothing ever reads that string. Caching still happens -- the memoize decorator
does it on the next lookup, which is what `test_the_cached_wrapper_serves_a_stale
_id_when_the_url_lookup_can_no_longer_find_it` proves -- but the explicit
write-back buys nothing except a wasted entry. Reported, not fixed: this file
may not change app/.

Under conftest's `CACHE_TYPE = 'NullCache'` every memoize read misses and every
write is discarded, so `find_actor_or_create_cached` recomputes on every call and
any "second call is cached" assertion passes for the wrong reason. The caching
test installs a real `SimpleCache` for its own duration so that its claim is
about caching rather than about the database answering twice.
"""

import json
from urllib.parse import urlparse

import httpx
import pytest
from cachelib import SimpleCache
from flask import current_app

from app import cache, db
from app.activitypub.signature import HttpSignature, RsaKeys
from app.activitypub.util import find_actor_or_create, find_actor_or_create_cached
from app.models import Community, User, utcnow
from tests.factories import make_instance, make_user

REMOTE_DOMAIN = 'remotepeer.example.org'
REMOTE_COMMUNITY_URL = f'https://{REMOTE_DOMAIN}/c/piefed_meta'

# Shaped after docs/activitypub_examples/communities.md -- the actual response
# piefed.social returns for /c/piefed_meta, which is the object the original
# version of this test fetched over the network -- with the host rewritten to a
# reserved example domain. `icon` is deliberately omitted: actor_json_to_model
# attaches a File for it and then calls make_image_sizes, which fetches the
# image, and none of the assertions here are about avatars.
REMOTE_COMMUNITY_ACTOR = {
    '@context': ['https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'],
    'attributedTo': f'{REMOTE_COMMUNITY_URL}/moderators',
    'endpoints': {'sharedInbox': f'https://{REMOTE_DOMAIN}/inbox'},
    'featured': f'{REMOTE_COMMUNITY_URL}/featured',
    'followers': f'{REMOTE_COMMUNITY_URL}/followers',
    'id': REMOTE_COMMUNITY_URL,
    'inbox': f'{REMOTE_COMMUNITY_URL}/inbox',
    'moderators': f'{REMOTE_COMMUNITY_URL}/moderators',
    'name': 'PieFed Meta',
    'newModsWanted': False,
    'outbox': f'{REMOTE_COMMUNITY_URL}/outbox',
    'postingRestrictedToMods': False,
    'preferredUsername': 'piefed_meta',
    'privateMods': False,
    'publicKey': {
        'id': f'{REMOTE_COMMUNITY_URL}#main-key',
        'owner': REMOTE_COMMUNITY_URL,
        'publicKeyPem': '-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n',
    },
    'published': '2024-01-04T08:55:20.668181+00:00',
    'sensitive': False,
    'source': {'content': 'Discuss PieFed project direction.', 'mediaType': 'text/markdown'},
    'summary': '<p>Discuss PieFed project direction.</p>',
    'type': 'Group',
    'updated': '2024-11-12T05:01:43.466282+00:00',
    'url': REMOTE_COMMUNITY_URL,
}


@pytest.fixture
def local_instance():
    """The Instance row for this server, at id 1.

    make_user(None, ...) writes instance_id=1, and instance_id carries a foreign
    key, so something has to put a row there after db_session's truncation.
    """
    return make_instance(current_app.config['SERVER_NAME'], software='piefed')


@pytest.fixture
def remote_instance():
    """The Instance row for the peer this file's remote actors live on.

    Created up front rather than left to find_instance_id, which inserts a
    sparse row for an unknown domain and then fires new_instance_profile_task --
    eagerly, under this suite's Celery config -- to go and fetch its nodeinfo.
    """
    return make_instance(REMOTE_DOMAIN, software='piefed')


def make_local_actor(name):
    """A local User reachable by its own actor URL, as production creates one.

    make_user(..., local=True) leaves ap_profile_id NULL, but a local user
    PieFed itself creates carries `https://<server>/u/<name lowercased>` there
    (app/cli.py sets exactly that when it promotes the admin account).
    find_local_user matches on that column, so a factory-default local user is
    invisible to find_actor_or_create.
    """
    user = make_user(None, name, local=True)
    user.ap_profile_id = f'https://{current_app.config["SERVER_NAME"]}/u/{name.lower()}'
    db.session.commit()
    return user


def make_remote_actor(instance, name):
    """A remote User that will not be refreshed on lookup.

    schedule_actor_refresh refreshes any non-local actor whose ap_fetched_at is
    NULL or older than a day, and the refresh is an outbound fetch that runs
    inline under eager Celery. A remote row that has just been fetched carries
    utcnow() there; make_user leaves it NULL.
    """
    user = make_user(instance, name)
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def test_find_actor_or_create(app, db_session, site, local_instance, remote_instance, http_mock):
    server_name = app.config['SERVER_NAME']
    local_user = make_local_actor('localadmin')
    remote_user = make_remote_actor(remote_instance, 'wakko')
    http_mock.get(REMOTE_COMMUNITY_URL).mock(
        return_value=httpx.Response(200, json=REMOTE_COMMUNITY_ACTOR))

    # A local URL resolves to the local user.
    found = find_actor_or_create(f'https://{server_name}/u/localadmin', create_if_not_found=False)
    assert isinstance(found, User)
    assert found.id == local_user.id

    # A remote URL for an actor nobody has heard of, with creation refused.
    assert find_actor_or_create('https://notreal.example.com/u/fake', create_if_not_found=False) is None

    # community_only against a local URL that names no community.
    assert find_actor_or_create(f'https://{server_name}/c/asdfasdf',
                                community_only=True,
                                create_if_not_found=False) is None

    # community_only against a real remote community, with creation allowed:
    # the actor is fetched and stored.
    community = find_actor_or_create(REMOTE_COMMUNITY_URL,
                                     community_only=True,
                                     create_if_not_found=True)
    assert isinstance(community, Community)
    assert community.name == 'piefed_meta'
    assert community.title == 'PieFed Meta'
    assert community.ap_profile_id == REMOTE_COMMUNITY_URL
    assert community.instance_id == remote_instance.id
    assert db.session.query(Community).filter_by(ap_profile_id=REMOTE_COMMUNITY_URL).one().id == community.id

    # feed_only against a local URL that names no feed.
    assert find_actor_or_create(f'https://{server_name}/f/asdfasdf',
                                feed_only=True,
                                create_if_not_found=False) is None

    # A whatever@server.tld actor string resolves through the webfinger-shaped
    # branch. See the module docstring: this is asserted against a REMOTE handle
    # because the local form cannot resolve -- find_actor_by_url matches ap_id
    # there, and a local user's ap_id is NULL.
    by_handle = find_actor_or_create(f'wakko@{REMOTE_DOMAIN}', create_if_not_found=True)
    assert isinstance(by_handle, User)
    assert by_handle.id == remote_user.id


def test_a_local_users_own_handle_does_not_resolve(app, db_session, site, local_instance):
    """The half of the original whatever@server.tld assertion that does not hold.

    Recorded as behaviour rather than dropped: find_actor_by_url's '@' branch
    filters on User.ap_id, which is NULL for every local user, so the local user
    created two lines below is not found by the handle that names it. With
    create_if_not_found refused there is nowhere else for the call to go. The
    original test asked this question with create_if_not_found allowed, which
    sent it to webfinger this instance's own domain over the network.
    """
    server_name = app.config['SERVER_NAME']
    local_user = make_local_actor('localadmin')

    assert find_actor_or_create(f'localadmin@{server_name}', create_if_not_found=False) is None
    # ... while the same user's actor URL finds it, so the row itself is fine.
    assert find_actor_or_create(f'https://{server_name}/u/localadmin',
                                create_if_not_found=False).id == local_user.id


@pytest.fixture
def real_cache(app, monkeypatch):
    """Swap conftest's NullCache for a working one, for one test.

    CACHE_TYPE = 'NullCache' makes every Flask-Caching read miss and every write
    a no-op, so @cache.memoize recomputes on every call and a "the second call
    was served from cache" assertion passes whether or not any caching happened.
    Flask-Caching resolves its backend through app.extensions['cache'][cache] on
    every access, so replacing that entry is enough, and monkeypatch puts the
    NullCache back afterwards.
    """
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    return cache


def test_find_actor_or_create_cached(app, db_session, site, local_instance, remote_instance,
                                     http_mock, real_cache):
    server_name = app.config['SERVER_NAME']
    local_user = make_local_actor('localadmin')
    remote_user = make_remote_actor(remote_instance, 'wakko')
    http_mock.get(REMOTE_COMMUNITY_URL).mock(
        return_value=httpx.Response(200, json=REMOTE_COMMUNITY_ACTOR))

    # A local URL, twice: the second call is the memoized one.
    found = find_actor_or_create_cached(f'https://{server_name}/u/localadmin', create_if_not_found=False)
    assert isinstance(found, User)
    assert found.id == local_user.id
    again = find_actor_or_create_cached(f'https://{server_name}/u/localadmin', create_if_not_found=False)
    assert again is not None
    assert again.id == local_user.id

    # A remote URL for an actor nobody has heard of, with creation refused.
    assert find_actor_or_create_cached('https://notreal.example.com/u/fake',
                                       create_if_not_found=False) is None

    # community_only against a local URL that names no community.
    assert find_actor_or_create_cached(f'https://{server_name}/c/asdfasdf',
                                       community_only=True,
                                       create_if_not_found=False) is None

    # community_only against a real remote community, with creation allowed.
    community = find_actor_or_create_cached(REMOTE_COMMUNITY_URL,
                                            community_only=True,
                                            create_if_not_found=True)
    assert isinstance(community, Community)
    assert community.name == 'piefed_meta'
    community_id = community.id

    # The same lookup again returns the same community.
    cached_community = find_actor_or_create_cached(REMOTE_COMMUNITY_URL,
                                                   community_only=True,
                                                   create_if_not_found=False)
    assert cached_community is not None
    assert cached_community.id == community_id

    # feed_only against a local URL that names no feed.
    assert find_actor_or_create_cached(f'https://{server_name}/f/asdfasdf',
                                       feed_only=True,
                                       create_if_not_found=False) is None

    # A whatever@server.tld actor string, against a remote handle for the reason
    # given in the module docstring.
    by_handle = find_actor_or_create_cached(f'wakko@{REMOTE_DOMAIN}', create_if_not_found=True)
    assert isinstance(by_handle, User)
    assert by_handle.id == remote_user.id

    # What comes back is a live model attached to this session, not a pickled
    # copy: the wrapper caches an id and re-fetches through db.session.get.
    assert found in db.session
    assert cached_community in db.session


def test_the_cached_wrapper_serves_a_stale_id_when_the_url_lookup_can_no_longer_find_it(
        app, db_session, site, local_instance, remote_instance, http_mock, real_cache):
    """The assertion that a real cache passes and NullCache does not.

    "call it twice and get the same answer" cannot tell a cache hit from the
    database simply answering the same question twice. This moves the community
    out from under its own actor URL after priming the cache, so the only way to
    still get it back is the cached (id, type) pair. Under conftest's NullCache
    this returns None.
    """
    http_mock.get(REMOTE_COMMUNITY_URL).mock(
        return_value=httpx.Response(200, json=REMOTE_COMMUNITY_ACTOR))

    community = find_actor_or_create_cached(REMOTE_COMMUNITY_URL,
                                            community_only=True,
                                            create_if_not_found=True)
    community_id = community.id
    # Priming call: the creation above happens in the wrapper's fallback path,
    # whose write-back key nothing reads (see the module docstring). It is this
    # lookup that populates the memoized entry.
    assert find_actor_or_create_cached(REMOTE_COMMUNITY_URL,
                                       community_only=True,
                                       create_if_not_found=False).id == community_id

    community.ap_profile_id = f'https://{REMOTE_DOMAIN}/c/renamed_out_of_the_way'
    db.session.commit()

    # The URL lookup can no longer reach it...
    assert find_actor_or_create(REMOTE_COMMUNITY_URL,
                                community_only=True,
                                create_if_not_found=False) is None
    # ... but the cached id still does.
    from_cache = find_actor_or_create_cached(REMOTE_COMMUNITY_URL,
                                             community_only=True,
                                             create_if_not_found=False)
    assert isinstance(from_cache, Community)
    assert from_cache.id == community_id


@pytest.mark.parametrize('change', ['banned', 'deleted'])
def test_the_cached_wrapper_refuses_a_remote_user_banned_or_deleted_after_caching(
        app, db_session, site, local_instance, remote_instance, real_cache, change):
    """D59, fixed. A cache hit re-fetches the row by primary key, and that
    re-fetch used to apply no filter at all: an actor banned (or deleted)
    within ten minutes of being resolved was still returned to every caller
    that did not re-check for itself. The hit now applies the same actor-level
    checks the uncached path's `validate_remote_actor` does.

    The priming call is asserted, so the refusal below is a refusal of a
    cached id and not a miss.
    """
    url = f'https://{REMOTE_DOMAIN}/u/wakko'
    user = make_remote_actor(remote_instance, 'wakko')
    user.ap_profile_id = url
    db.session.commit()
    assert find_actor_or_create_cached(url, create_if_not_found=False).id == user.id

    setattr(user, change, True)
    db.session.commit()

    assert find_actor_or_create_cached(url, create_if_not_found=False) is None


def test_the_cached_wrapper_refuses_a_local_user_banned_after_caching(
        app, db_session, site, local_instance, real_cache):
    """D59, fixed, local arm. The uncached path's `find_local_user` filters
    `banned=False`; the cache hit now does too."""
    url = f'https://{app.config["SERVER_NAME"]}/u/localadmin'
    user = make_local_actor('localadmin')
    assert find_actor_or_create_cached(url, create_if_not_found=False).id == user.id

    user.banned = True
    db.session.commit()

    assert find_actor_or_create_cached(url, create_if_not_found=False) is None


def test_signed_request_async(app):
    """Test the signed_request function with send_via_async=True to verify signature generation"""
    private_key, public_key = RsaKeys.generate_keypair()

    test_body = {
        "type": "Create",
        "id": "https://example.com/activities/1",
        "actor": "https://example.com/users/testuser",
        "object": {
            "type": "Note",
            "content": "Test content"
        }
    }

    test_uri = "https://remote.example.com/inbox"
    test_key_id = "https://example.com/users/testuser#main-key"

    # Called three times: the first is a cache miss for the parsed key, the
    # other two exercise the hit path.
    for _ in range(3):
        result = HttpSignature.signed_request(
            uri=test_uri,
            body=test_body,
            private_key=private_key,
            key_id=test_key_id,
            content_type="application/activity+json",
            method="post",
            timeout=10,
            send_via_async=True
        )

    # Verify the result is a tuple of (uri, headers, body_bytes)
    assert isinstance(result, tuple)
    assert len(result) == 3

    returned_uri, headers, body_bytes = result

    # Verify the URI is returned correctly
    assert returned_uri == test_uri

    # Verify body_bytes is properly JSON-encoded
    assert isinstance(body_bytes, bytes)
    decoded_body = json.loads(body_bytes.decode('utf8'))
    assert decoded_body['type'] == 'Create'
    assert decoded_body['id'] == test_body['id']
    assert '@context' in decoded_body  # Should be added automatically

    # Verify required headers exist
    assert 'Host' in headers
    assert 'Date' in headers
    assert 'Digest' in headers
    assert 'Content-Type' in headers
    assert 'Signature' in headers
    assert 'User-Agent' in headers

    # Verify header values
    assert headers['Host'] == 'remote.example.com'
    assert headers['Content-Type'] == 'application/activity+json'
    assert headers['Digest'].startswith('SHA-256=')

    # Verify the Signature header format
    signature_header = headers['Signature']
    assert 'keyId=' in signature_header
    assert 'headers=' in signature_header
    assert 'signature=' in signature_header
    assert 'algorithm=' in signature_header
    assert test_key_id in signature_header
    assert 'rsa-sha256' in signature_header

    # Parse and verify the signature details
    signature_details = HttpSignature.parse_signature(signature_header)
    assert signature_details['keyid'] == test_key_id
    assert signature_details['algorithm'] == 'rsa-sha256'
    assert isinstance(signature_details['signature'], bytes)
    assert len(signature_details['signature']) > 0

    # Verify the required headers are included in the signature
    required_headers = ['host', 'date', 'digest', 'content-type']
    for header in required_headers:
        assert header in [h.lower() for h in signature_details['headers']]

    # Verify the digest is correct
    expected_digest = HttpSignature.calculate_digest(body_bytes)
    assert headers['Digest'] == expected_digest

    # Verify the signature can be verified with the public key.
    # Reconstruct the signed string - must include all headers in the exact order
    uri_parts = urlparse(test_uri)

    signed_string_parts = []
    for header_name in signature_details['headers']:
        if header_name == '(request-target)':
            signed_string_parts.append(f"(request-target): post {uri_parts.path}")
        elif header_name == 'host':
            signed_string_parts.append(f"host: {headers['Host']}")
        elif header_name == 'date':
            signed_string_parts.append(f"date: {headers['Date']}")
        elif header_name == 'digest':
            signed_string_parts.append(f"digest: {headers['Digest']}")
        elif header_name == 'content-type':
            signed_string_parts.append(f"content-type: {headers['Content-Type']}")

    signed_string = "\n".join(signed_string_parts)

    # verify_signature raises on a bad signature and returns None on a good one,
    # so letting it raise here is the assertion.
    HttpSignature.verify_signature(signature_details['signature'], signed_string, public_key)
