"""Each federating domain is its own ActivityPub server: its own instance actor key and the URLs that name what it
federates (plan C3 task 1). hell.cloud, the default, keeps today's strings exactly and Site row 1's key."""
import importlib.util
import re
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import inspect, text

from app import db
from app.federation_urls import (activity_id, domain_of_url, instance_actor_signing, instance_actor_url,
                                 owner_base, owner_domain)
from app.models import Site, SiteDomain
from app.storefront import clear_domain_cache, default_domain, domain_for_host
from tests.factories import (a_keypair, make_chat_message, make_feed, make_instance, make_post, make_post_reply,
                             make_post_reply_vote, make_post_vote, make_user)
from tests.two_domains import HELL, ONION, onion_request, two_domains  # noqa: F401

SHOP = 'shop.example'
MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd3a7e5c1f9b2_site_domain_keys.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('site_domain_keys_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shop(app, two_domains):
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    yield domain_for_host(SHOP)
    clear_domain_cache()


@pytest.fixture
def keyed_site(app, db_session):
    site = Site(name='Test Site', blocked_phrases='')
    site.private_key, site.public_key = a_keypair()
    db.session.add(site)
    db.session.commit()
    return site


def _signs_with(private_pem: str, public_pem: str) -> bool:
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    public = serialization.load_pem_public_key(public_pem.encode())
    signature = private.sign(b'probe', padding.PKCS1v15(), hashes.SHA256())
    try:
        public.verify(signature, b'probe', padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return False
    return True


# --- the default domain: today's strings ---

def test_the_default_domains_helpers_are_todays_literals(app, keyed_site, two_domains):
    server_url = app.config['SERVER_URL']
    default = default_domain()
    assert instance_actor_url(default) == f'{server_url}/actor'
    assert instance_actor_signing(default) == (keyed_site.private_key, f'{server_url}/actor#main-key')
    assert activity_id('announce', default, 'Ab3') == f'{server_url}/activities/announce/Ab3'
    hell_user = two_domains.local_user('hellion', two_domains.domains[0])
    assert owner_base(hell_user) == server_url
    assert re.fullmatch(rf'{re.escape(server_url)}/activities/like/[0-9A-Za-z]{{15}}', activity_id('like', hell_user))
    assert activity_id('like', hell_user) != activity_id('like', hell_user)


def test_a_synthesised_default_signs_with_site_one(app, keyed_site):
    """With no site_domain rows the default is synthesised (no row id): it still reads Site row 1's key."""
    assert instance_actor_signing(default_domain()) == (keyed_site.private_key,
                                                        f"{app.config['SERVER_URL']}/actor#main-key")


# --- whose domain a row is federated from ---

def test_a_shop_rows_owner_is_the_shop(app, shop, two_domains):
    hell = two_domains.domains[0]
    bob, hell_bob = two_domains.local_user('bob', shop), two_domains.local_user('hellbob', hell)
    art = two_domains.local_community('art', shop)
    post = make_post(art, bob, ap_id=f'https://{SHOP}/post/1')
    reply = make_post_reply(post, bob)
    message = make_chat_message(bob, hell_bob, ap_id=f'https://{SHOP}/private_message/1')
    hell_post = make_post(art, hell_bob, ap_id='https://test.piefed.local/post/2')
    post_vote, reply_vote = make_post_vote(bob, hell_post, 1.0), make_post_reply_vote(bob, reply, 1.0)
    for row in (bob, art, post, reply, message, post_vote, reply_vote, shop):
        assert owner_domain(row) == shop, row
        assert owner_base(row) == f'https://{SHOP}', row
    # A row on the shop's community is the author's: hell.cloud's own post there is hell.cloud's
    assert owner_domain(hell_post) == hell
    assert owner_domain(make_post_vote(hell_bob, post, 1.0)) == hell


def test_a_remote_authors_rows_have_no_owner_here(app, shop, two_domains):
    peer = make_instance('peer.example')
    remote = make_user(peer, 'remote')
    art = two_domains.local_community('art', shop)
    post = make_post(art, remote, ap_id='https://peer.example/p/1')
    for row in (remote, post, make_post_reply(post, remote), make_post_vote(remote, post, 1.0)):
        assert owner_domain(row) is None, row
        with pytest.raises(ValueError):
            owner_base(row)
        with pytest.raises(ValueError):
            activity_id('like', row)


def test_a_feed_is_the_default_domains(app, shop, two_domains):
    local_feed = make_feed(two_domains.instance, 'myfeed', local=True)
    assert owner_domain(local_feed) == default_domain()
    assert owner_domain(make_feed(make_instance('peer.example'), 'theirs')) is None


def test_an_unknown_owner_is_an_error(app, shop):
    with pytest.raises(TypeError):
        owner_domain(object())


def test_the_owner_not_the_request_decides(app, shop, two_domains):
    bob = two_domains.local_user('bob', shop)
    assert owner_domain(bob) == shop                   # no request at all: a Celery task
    with onion_request(app):
        assert owner_domain(bob) == shop               # a request on another storefront
        assert activity_id('follow', bob, 'tok') == f'https://{SHOP}/activities/follow/tok'


# --- which hosted domain a URL names ---

@pytest.mark.parametrize('url, expected', [
    (f'https://{SHOP}/u/bob', SHOP),
    (f'https://{SHOP.upper()}/c/art', SHOP),
    (f'http://{SHOP}/post/1', SHOP),
    ('https://test.piefed.local/post/1', HELL),
    (f'https://{SHOP}.evil/u/bob', None),
    (f'https://evil.{SHOP}/u/bob', None),
    (f'https://evil.example/{SHOP}', None),
    (f'https://bob@{SHOP}/u/bob', None),
    (f'https://{SHOP}:8443/u/bob', None),
    ('https://peer.example/u/bob', None),
    ('not a url', None),
    ('https://[::1/x', None),
    ('', None),
    (None, None),
])
def test_domain_of_url_is_an_exact_authority_match(app, shop, url, expected):
    found = domain_of_url(url)
    assert (found.domain if found else None) == expected


# --- the keys ---

def test_a_new_sister_domain_gets_its_own_key_pair_and_the_default_none(app, shop):
    rows = {row.domain: row for row in db.session.query(SiteDomain)}
    for domain in (SHOP, ONION):
        row = rows[domain]
        assert row.private_key.startswith('-----BEGIN PRIVATE KEY-----'), domain
        assert _signs_with(row.private_key, row.public_key), domain
    assert rows[SHOP].private_key != rows[ONION].private_key
    assert (rows[HELL].private_key, rows[HELL].public_key) == (None, None)


def test_a_domain_given_a_key_keeps_it(app, db_session):
    private_key, public_key = a_keypair()
    db.session.add(SiteDomain(domain=SHOP, private_key=private_key, public_key=public_key))
    db.session.commit()
    row = db.session.query(SiteDomain).filter_by(domain=SHOP).one()
    assert (row.private_key, row.public_key) == (private_key, public_key)


def test_the_shops_instance_actor_signs_with_the_shops_key(app, keyed_site, shop):
    row = db.session.get(SiteDomain, shop.id)
    private_key, key_id = instance_actor_signing(shop)
    assert (private_key, key_id) == (row.private_key, f'https://{SHOP}/actor#main-key')
    assert instance_actor_url(shop) == f'https://{SHOP}/actor'
    assert _signs_with(private_key, row.public_key)
    assert not _signs_with(private_key, keyed_site.public_key)


def test_the_key_is_read_on_demand_not_cached(app, keyed_site, shop):
    """The cached DomainInfo carries no key: a rotated key is used at once."""
    assert not any('key' in field for field in type(shop)._fields)
    private_key, public_key = a_keypair()
    row = db.session.get(SiteDomain, shop.id)
    row.private_key, row.public_key = private_key, public_key
    db.session.commit()
    assert instance_actor_signing(shop)[0] == private_key


# --- the migration ---

def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('d3a7e5c1f9b2', 'c8f4d2a6b9e1')


def test_the_key_columns_are_nullable_text(app, db_session):
    with db.engine.connect() as connection:
        columns = {c['name']: c for c in inspect(connection).get_columns('site_domain')}
    for name in ('public_key', 'private_key'):
        assert columns[name]['nullable'] is True
        assert str(columns[name]['type']) == 'TEXT'


def _keys(connection):
    return {domain: (private_key, public_key) for domain, private_key, public_key in connection.execute(
        text('SELECT domain, private_key, public_key FROM site_domain'))}


def test_the_upgrade_backfills_every_sister_domain_and_is_idempotent(app, shop):
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert 'private_key' not in {c['name'] for c in inspect(connection).get_columns('site_domain')}
                migration.upgrade()
                first = _keys(connection)
                migration.backfill_keys(connection)   # run again: nothing that has a key changes
                again = _keys(connection)
                connection.execute(text('UPDATE site_domain SET private_key = NULL, public_key = NULL '
                                        'WHERE domain = :d'), {'d': ONION})
                migration.backfill_keys(connection)
                refilled = _keys(connection)
        finally:
            transaction.rollback()
    assert first[HELL] == (None, None)
    for domain in (SHOP, ONION):
        assert _signs_with(*first[domain]), domain
    assert first[SHOP] != first[ONION]
    assert again == first
    assert refilled[SHOP] == first[SHOP] and refilled[HELL] == (None, None)
    assert _signs_with(*refilled[ONION]) and refilled[ONION] != first[ONION]
