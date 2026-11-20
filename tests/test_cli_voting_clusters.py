"""flask voting_clusters (app/cli.py): a group of accounts voting together is found, described, and reported as a
boost ring when its internal votes go to one member."""
import pytest

from app import cli
from tests.factories import make_community, make_instance, make_post, make_post_vote, make_user

# Thresholds a five-account fixture can meet: the defaults are tuned for a real instance's months of votes
SMALL = ['--min-shared', '2', '--min-communities', '1', '--min-logp', '0', '--min-clique', '3',
         '--min-promoted', '1', '--min-inward', '0.1']


@pytest.fixture
def commands(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app.test_cli_runner()


@pytest.fixture
def ring(db_session):
    """booster1-3 and hub all upvote outsider's four posts (so the four form one group), and booster1-3 upvote hub's
    four posts: hub is the group's beneficiary."""
    local = make_instance('test.piefed.local', software='piefed')
    hub, outsider = make_user(local, 'hub', local=True), make_user(local, 'outsider', local=True)
    boosters = [make_user(local, f'booster{n}', local=True) for n in (1, 2, 3)]
    community = make_community('ringland')   # after the users: it is owned by user 1
    for n in range(4):
        shared = make_post(community, outsider, ap_id=f'https://test.piefed.local/post/shared{n}')
        for voter in [hub, *boosters]:
            make_post_vote(voter, shared, 1)
        boosted = make_post(community, hub, ap_id=f'https://test.piefed.local/post/boosted{n}')
        for voter in boosters:
            make_post_vote(voter, boosted, 1)
    return hub, boosters


def test_a_group_whose_votes_go_to_one_member_is_reported_as_a_ring(commands, ring):
    hub, _boosters = ring
    result = commands.invoke(args=['voting_clusters', *SMALL])
    assert result.exception is None, result.output
    assert '1 groups of >= 3 accounts' in result.output
    assert 'hub' in result.output


def test_a_group_below_the_promoted_floor_is_no_ring(commands, ring):
    """The same group, with more votes asked of its beneficiary than it has (12): grouped, but no ring."""
    result = commands.invoke(args=['voting_clusters', *SMALL[:-4], '--min-promoted', '100', '--min-inward', '0.1'])
    assert result.exception is None, result.output
    assert '1 groups of >= 3 accounts' in result.output
    assert '0 of them concentrate on one account' in result.output
