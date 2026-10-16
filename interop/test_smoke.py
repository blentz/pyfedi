"""The harness itself works: TLS, DNS, webfinger and a federated follow both ways (spec §8.1)."""
import httpx
import pytest

from interop.clients import CA, tokens, wait_for

pytestmark = pytest.mark.interop


def test_bob_on_mastodon_follows_alice_on_piefed(bob_follows_alice):
    assert bob_follows_alice['acct'] == 'alice@piefed.test'


def test_peertube_token_is_carols(peertube):
    assert peertube.me()['username'] == 'carol'


def test_bob_on_mastodon_follows_carol_on_peertube(mastodon):
    """PeerTube fetches bob's key from mastodon.test to check the Follow, then delivers its Accept there: both
    directions of TLS through the harness CA."""
    account = wait_for(lambda: mastodon.resolve_account('carol@peertube.test'))
    mastodon.follow(account['id'])
    wait_for(lambda: mastodon.relationship(account['id'])['following'])


def test_castopod_serves_its_podcast_and_episode():
    """seed_castopod.py's podcast and episode, fetched as ActivityPub through the harness CA (spec §9 step 5)."""
    seeded = tokens()
    ap = httpx.Client(verify=CA, timeout=10, headers={'Accept': 'application/activity+json'})
    actor = ap.get(seeded['castopod_podcast']).raise_for_status().json()
    assert (actor['type'], actor['preferredUsername']) == ('Person', 'dave')
    episode = ap.get(seeded['castopod_episode']).raise_for_status().json()
    assert episode['type'] == 'PodcastEpisode'
    assert episode['audio']['url']['mediaType'] == 'audio/mpeg'
