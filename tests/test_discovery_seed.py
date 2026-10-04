"""Interop D24, local validation: `flask discovery-seed-fixtures` loads a PeerTube channel, a Castopod
podcast with RSS credits and Mastodon/Pixelfed directory people from fixture files, with no network."""
import pytest

from app import cli
from app.discovery.credits import podcast_api_credits, podcast_byline
from app.discovery.seed import seed_from_fixtures
from app.models import Community, DiscoveryEntry, Instance, Post, User
from tests.factories import make_instance, make_user

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
    assert DiscoveryEntry.query.count() == 8
    community = Community.query.filter_by(ap_profile_id='https://pod.example/@mypodcast').one()
    post = Post.query.filter_by(community_id=community.id).one()
    assert [(c['name'], c['role']) for c in post.extensions['podcast']['credits']] == [
        ('Ann Host', 'host'), ('Ben Cohost', 'host'), ('Cara Guest', 'guest')]


def test_only_the_credit_that_vouches_back_is_verified(app, db_session, http_mock):
    seed_from_fixtures()

    post = Post.query.one()
    ann, ben, cara = post.extensions['podcast']['credits']
    seeded = User.query.filter_by(ap_profile_id='https://people.example/users/ann').one()
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
    assert DiscoveryEntry.query.count() == 8
    assert Post.query.count() == 1
    assert User.query.filter_by(ap_profile_id='https://people.example/users/ann').count() == 1


def test_the_credited_account_is_a_remote_fixture_user_and_no_local_user_is_touched(app, db_session, http_mock):
    local_ann = make_user(make_instance(app.config['SERVER_NAME'], software='piefed'), 'ann', local=True)
    before = {c.name: getattr(local_ann, c.name) for c in User.__table__.columns}

    seed_from_fixtures()

    db_session.refresh(local_ann)
    assert {c.name: getattr(local_ann, c.name) for c in User.__table__.columns} == before
    assert local_ann.extra_fields.count() == 0
    assert User.query.filter(User.ap_id == None).all() == [local_ann]
    credited = User.query.filter_by(ap_profile_id='https://people.example/users/ann').one()
    assert (credited.ap_id, credited.ap_domain) == ('ann@people.example', 'people.example')
    assert Post.query.one().extensions['podcast']['credits'][0]['user_id'] == credited.id


def test_the_seed_creates_no_local_user(app, db_session, http_mock):
    seed_from_fixtures()

    assert User.query.filter(User.ap_id == None).count() == 0


def test_the_credited_account_belongs_to_its_own_hosts_instance_not_instance_1(app, db_session, http_mock):
    make_instance('filler.example')
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    assert local.id != 1

    seed_from_fixtures()

    credited = User.query.filter_by(ap_profile_id='https://people.example/users/ann').one()
    assert credited.instance_id == Instance.query.filter_by(domain='people.example').one().id
    assert credited.instance_id not in (1, local.id)


def test_the_help_names_everything_the_command_creates(app, db_session):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures', '--help'])

    help_text = ' '.join(result.output.split())
    for created in ('discovery entries', 'podcast user', 'podcast community', 'episode post', 'credited remote user'):
        assert created in help_text


def test_the_output_lists_everything_the_command_created(app, db_session, http_mock):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures', '--force'])

    community = Community.query.filter_by(ap_profile_id='https://pod.example/@mypodcast').one()
    post = Post.query.one()
    credited = User.query.filter_by(ap_profile_id='https://people.example/users/ann').one()
    assert result.output.splitlines() == [
        'discovery entries: 8',
        f'podcast user: {post.user_id}',
        f'podcast community: {community.id}',
        f'episode post: {post.id}',
        f'credited remote user: {credited.id}',
    ]
