"""Interop D24, local validation: `flask discovery-seed-fixtures` loads a PeerTube channel, a Castopod
podcast with RSS credits and Mastodon/Pixelfed directory people from fixture files, with no network."""
import pytest

from app import cli
from app.discovery.credits import podcast_api_credits, podcast_byline
from app.discovery.seed import seed_from_fixtures
from app.models import Community, DiscoveryEntry, Post, User

pytestmark = pytest.mark.usefixtures('site')


def test_the_command_refuses_outside_debug_without_force(app, db_session):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures'])

    assert 'Refusing' in result.output
    assert result.exit_code != 0
    assert DiscoveryEntry.query.count() == 0


def test_the_seed_loads_every_platform_and_a_podcast_with_credits(app, db_session, http_mock):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures', '--force'])

    assert result.exception is None, result.exception
    assert sorted({entry.platform for entry in DiscoveryEntry.query}) == ['castopod', 'mastodon', 'peertube', 'pixelfed']
    assert DiscoveryEntry.query.count() == 7
    community = Community.query.filter_by(ap_profile_id='https://pod.example/@mypodcast').one()
    post = Post.query.filter_by(community_id=community.id).one()
    assert [(c['name'], c['role']) for c in post.extensions['podcast']['credits']] == [
        ('Ann Host', 'host'), ('Ben Cohost', 'host'), ('Cara Guest', 'guest')]


def test_only_the_credit_that_vouches_back_is_verified(app, db_session, http_mock):
    seed_from_fixtures()

    post = Post.query.one()
    ann, ben, cara = post.extensions['podcast']['credits']
    seeded = User.query.filter_by(user_name='ann').one()
    assert (ann['verified'], ann['user_id'], ann['profile_url']) == (True, seeded.id, seeded.public_url())
    for unverified in (ben, cara):
        assert unverified.get('verified') is None and unverified['user_id'] is None
        assert unverified['profile_url'] is None
    assert [(c['name'], c['role']) for c in podcast_api_credits(post)] == [
        (seeded.display_name(), 'host'), ('Ben Cohost', 'host'), ('Cara Guest', 'guest')]
    assert [c['href'] for c in podcast_byline(post)['hosts']] == [f'/u/{seeded.link()}', None]


def test_seeding_twice_changes_nothing(app, db_session, http_mock):
    first = seed_from_fixtures()
    second = seed_from_fixtures()

    assert first == second
    assert DiscoveryEntry.query.count() == 7
    assert Post.query.count() == 1
    assert User.query.filter_by(user_name='ann').count() == 1
