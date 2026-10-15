"""The harness itself works: TLS, DNS, webfinger and a federated follow both ways (spec §8.1)."""
import pytest

pytestmark = pytest.mark.interop


def test_bob_on_mastodon_follows_alice_on_piefed(bob_follows_alice):
    assert bob_follows_alice['acct'] == 'alice@piefed.test'
