import pytest

from interop.clients import Mastodon, PeerTube, PieFed, tokens, wait_for


@pytest.fixture(scope='session')
def mastodon():
    return Mastodon(tokens()['mastodon_bob'])


@pytest.fixture(scope='session')
def peertube():
    return PeerTube(tokens()['peertube_carol'])


@pytest.fixture(scope='session')
def piefed():
    return PieFed('alice', 'interop-alice-pw')


@pytest.fixture(scope='session')
def bob_follows_alice(mastodon):
    account = wait_for(lambda: mastodon.resolve_account('alice@piefed.test'))
    mastodon.follow(account['id'])
    wait_for(lambda: mastodon.relationship(account['id'])['following'])
    return account
