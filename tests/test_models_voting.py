"""Voting on a post and on a comment, in every transition.

Sub-project 110 -- `Post.vote` and `PostReply.vote` in `app/models.py`. One
method each, and between them they hold the score arithmetic, the reputation
arithmetic, the emoji reactions, the "spicy" early-vote amplification, and the
three transitions a second vote can make: the same way again (which removes
it), the other way (which reverses it), or a reversal the API asks for by
sending 0.

Nothing here is reached by reading the method: `score -= existing_vote.effect`
and `score += existing_vote.effect * 2` are correct only for the combination of
signs they sit under, and the only way to say so is to vote twice and look.

Five defects, all in the reputation arithmetic and the permission gates rather
than in the vote counts, which were right everywhere:

* D1302, both models: a reversal subtracted the old vote's reputation and never
  applied the new one, so the score moved by 2 and the reputation by 1. An
  author's reputation depended on the order a voter clicked in.
* D1303, `Post.vote`: the whole reputation update was skipped in a low-quality
  community, where only UPVOTES are meant to earn nothing, so a downvote taken
  back there kept costing the author forever.
* D1304: `PostReply.vote` had no downvote refusal for an author who has blocked
  the voter, which `Post.vote` has always had.
* D1305: `PostReply.vote` had no low-quality exemption either, so the flag whose
  label reads "upvotes in here don't add to reputation" held for posts only.
* D1306, `app/shared/reply.py`: the WEB path of `vote_for_reply` called neither
  `can_upvote` nor `can_downvote` -- its API path called both, and
  `vote_for_post` gates both paths.
"""
import pytest
from flask import current_app, g

from app import db
from app.models import (Post, PostReply, PostReplyVote, PostVote, Site, User,
                        UserBlock)
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    voter = api_baseline.user3
    db.session.commit()
    make_community_member(author, community)
    make_community_member(voter, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    post.up_votes = 0
    post.down_votes = 0
    post.score = 0
    db.session.commit()
    reply = make_post_reply(post, author, body='a reply')
    reply.up_votes = 0
    reply.down_votes = 0
    reply.score = 0
    db.session.commit()
    # The amplification is what the instance configures; 1 keeps the
    # arithmetic in these tests readable, and its own tests set it back.
    for key in ('SPICY_UNDER_10', 'SPICY_UNDER_30', 'SPICY_UNDER_60'):
        monkeypatch.setitem(current_app.config, key, 1)
    return SimpleNamespace(community=community, author=author, voter=voter,
                           post=post, reply=reply, baseline=api_baseline)


def counts(thing):
    db.session.expire_all()
    fresh = db.session.get(type(thing), thing.id)
    return (fresh.up_votes, fresh.down_votes, fresh.score)


def reputation_of(user_id):
    db.session.expire_all()
    return db.session.get(User, user_id).reputation


class TestVotingOnAPostForTheFirstTime:
    def test_an_upvote(self, env):
        env.post.vote(env.voter, 'upvote', None)
        assert counts(env.post) == (1, 0, 1)

    def test_a_downvote(self, env):
        env.post.vote(env.voter, 'downvote', None)
        assert counts(env.post) == (0, 1, -1)

    def test_an_upvote_is_stored_against_the_author(self, env):
        env.post.vote(env.voter, 'upvote', None)
        vote = PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one()
        assert vote.effect == 1
        assert vote.author_id == env.author.id

    def test_an_upvote_adds_to_the_author_s_reputation(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 11

    def test_a_downvote_takes_from_it(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_an_upvote_in_a_low_quality_community_earns_nothing(self, env):
        """The score still moves; the reputation does not."""
        env.community.low_quality = True
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10
        assert counts(env.post) == (1, 0, 1)

    def test_a_downvote_in_one_still_costs(self, env):
        env.community.low_quality = True
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_nothing_is_undone(self, env):
        assert env.post.vote(env.voter, 'upvote', None) is None


class TestVotingOnAPostAgain:
    def test_the_same_way_twice_removes_the_vote(self, env):
        env.post.vote(env.voter, 'upvote', None)
        undo = env.post.vote(env.voter, 'upvote', None)
        assert undo == 'Like'
        assert counts(env.post) == (0, 0, 0)
        assert PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).count() == 0

    def test_the_same_way_down_twice_removes_it_too(self, env):
        env.post.vote(env.voter, 'downvote', None)
        undo = env.post.vote(env.voter, 'downvote', None)
        assert undo == 'Dislike'
        assert counts(env.post) == (0, 0, 0)

    def test_up_then_down_reverses_it(self, env):
        env.post.vote(env.voter, 'upvote', None)
        undo = env.post.vote(env.voter, 'downvote', None)
        assert undo is None
        assert counts(env.post) == (0, 1, -1)
        assert PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one().effect == -1

    def test_down_then_up_reverses_it(self, env):
        env.post.vote(env.voter, 'downvote', None)
        env.post.vote(env.voter, 'upvote', None)
        assert counts(env.post) == (1, 0, 1)
        assert PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one().effect == 1

    def test_removing_an_upvote_takes_the_reputation_back(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10

    def test_reversing_an_upvote_costs_twice(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9


class TestTheReversalTheApiAsksFor:
    def test_a_reversal_of_an_upvote(self, env):
        env.post.vote(env.voter, 'upvote', None)
        undo = env.post.vote(env.voter, 'reversal', None)
        assert undo == 'Like'
        assert counts(env.post) == (0, 0, 0)

    def test_a_reversal_of_a_downvote(self, env):
        env.post.vote(env.voter, 'downvote', None)
        undo = env.post.vote(env.voter, 'reversal', None)
        assert undo == 'Dislike'
        assert counts(env.post) == (0, 0, 0)

    def test_a_reversal_of_nothing(self, env):
        assert env.post.vote(env.voter, 'reversal', None) is None
        assert counts(env.post) == (0, 0, 0)

    def test_a_direction_that_is_none_of_the_three(self, env):
        """Was an `assert`, which `python -O` deletes."""
        with pytest.raises(ValueError, match='unresolvable vote direction'):
            env.post.vote(env.voter, 'sideways', None)


class TestWhoMayDownvote:
    def test_an_author_who_has_blocked_the_voter(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        assert env.post.vote(env.voter, 'downvote', None) is None
        assert counts(env.post) == (0, 0, 0)

    def test_the_same_voter_may_still_upvote(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        assert counts(env.post) == (1, 0, 1)

    def test_an_author_who_has_blocked_the_voter_s_instance(self, env):
        from app.models import InstanceBlock
        db.session.add(InstanceBlock(user_id=env.author.id,
                                     instance_id=env.voter.instance_id))
        db.session.commit()
        assert env.post.vote(env.voter, 'downvote', None) is None
        assert counts(env.post) == (0, 0, 0)


class TestTheSpicyAmplification:
    """Early votes count for more, so that 'hot' moves at all on a new post."""

    def amplified(self, env, up_votes, down_votes, monkeypatch, **spices):
        for key, value in spices.items():
            monkeypatch.setitem(current_app.config, key, value)
        env.post.up_votes = up_votes
        env.post.down_votes = down_votes
        env.post.score = 0
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        return counts(env.post)[2]

    def test_the_first_ten_votes_count_most(self, env, monkeypatch):
        assert self.amplified(env, 0, 0, monkeypatch, SPICY_UNDER_10=3) == 3

    def test_the_next_twenty_count_less(self, env, monkeypatch):
        assert self.amplified(env, 15, 0, monkeypatch, SPICY_UNDER_30=2) == 2

    def test_the_next_thirty_less_again(self, env, monkeypatch):
        """2, not 1.5: `Post.score` is an Integer column, so a fractional
        SPICY_UNDER_* is rounded on the way to the database. Not a defect --
        but it does mean an instance that sets 1.5 buys the same amplification
        as one that sets 2 for the first vote on a post."""
        assert self.amplified(env, 45, 0, monkeypatch, SPICY_UNDER_60=1.5) == 2

    def test_after_sixty_a_vote_is_just_a_vote(self, env, monkeypatch):
        assert self.amplified(env, 70, 0, monkeypatch, SPICY_UNDER_60=1.5) == 1

    def test_a_downvote_is_amplified_too(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'SPICY_UNDER_30', 2)
        env.post.up_votes = 10
        env.post.score = 0
        db.session.commit()
        env.post.vote(env.voter, 'downvote', None)
        assert counts(env.post)[2] == -2


class TestAnEmojiReaction:
    def test_voting_with_one(self, env):
        env.post.vote(env.voter, 'upvote', ':tada:')
        vote = PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one()
        assert vote.emoji == ':tada:'

    def test_changing_it_without_changing_the_vote(self, env):
        env.post.vote(env.voter, 'upvote', ':tada:')
        undo = env.post.vote(env.voter, 'upvote', ':wave:')
        assert undo is None
        vote = PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one()
        assert vote.emoji == ':wave:'
        assert counts(env.post) == (1, 0, 1)

    def test_changing_it_on_a_downvote(self, env):
        env.post.vote(env.voter, 'downvote', ':tada:')
        env.post.vote(env.voter, 'downvote', ':wave:')
        assert PostVote.query.one().emoji == ':wave:'
        assert counts(env.post) == (0, 1, -1)

    def test_reacting_and_then_voting_the_other_way(self, env):
        env.post.vote(env.voter, 'upvote', ':tada:')
        env.post.vote(env.voter, 'downvote', ':tada:')
        assert counts(env.post) == (0, 1, -1)

    def test_the_reactions_are_summarised_on_the_post(self, env):
        env.post.vote(env.voter, 'upvote', ':tada:')
        db.session.expire_all()
        post = db.session.get(Post, env.post.id)
        assert post.emoji_reactions


class TestVotingOnAComment:
    def test_an_upvote(self, env):
        env.reply.vote(env.voter, 'upvote', None)
        assert counts(env.reply) == (1, 0, 1)

    def test_a_downvote(self, env):
        env.reply.vote(env.voter, 'downvote', None)
        assert counts(env.reply) == (0, 1, -1)

    def test_the_same_way_twice_removes_the_vote(self, env):
        env.reply.vote(env.voter, 'upvote', None)
        undo = env.reply.vote(env.voter, 'upvote', None)
        assert undo == 'Like'
        assert counts(env.reply) == (0, 0, 0)

    def test_the_same_way_down_twice_removes_it_too(self, env):
        env.reply.vote(env.voter, 'downvote', None)
        undo = env.reply.vote(env.voter, 'downvote', None)
        assert undo == 'Dislike'
        assert counts(env.reply) == (0, 0, 0)

    def test_up_then_down_reverses_it(self, env):
        env.reply.vote(env.voter, 'upvote', None)
        env.reply.vote(env.voter, 'downvote', None)
        assert counts(env.reply) == (0, 1, -1)

    def test_down_then_up_reverses_it(self, env):
        env.reply.vote(env.voter, 'downvote', None)
        env.reply.vote(env.voter, 'upvote', None)
        assert counts(env.reply) == (1, 0, 1)

    def test_a_reversal_of_an_upvote(self, env):
        env.reply.vote(env.voter, 'upvote', None)
        assert env.reply.vote(env.voter, 'reversal', None) == 'Like'
        assert counts(env.reply) == (0, 0, 0)

    def test_a_reversal_of_nothing(self, env):
        assert env.reply.vote(env.voter, 'reversal', None) is None

    def test_an_upvote_adds_to_the_author_s_reputation(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.reply.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 11

    def test_an_upvote_in_a_low_quality_community_earns_nothing(self, env):
        env.community.low_quality = True
        env.author.reputation = 10
        db.session.commit()
        env.reply.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10

    def test_an_author_who_has_blocked_the_voter(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        assert env.reply.vote(env.voter, 'downvote', None) is None
        assert counts(env.reply) == (0, 0, 0)

    def test_an_emoji_reaction(self, env):
        env.reply.vote(env.voter, 'upvote', ':tada:')
        assert PostReplyVote.query.filter_by(
            user_id=env.voter.id, post_reply_id=env.reply.id).one().emoji == \
            ':tada:'

    def test_changing_it_without_changing_the_vote(self, env):
        env.reply.vote(env.voter, 'upvote', ':tada:')
        assert env.reply.vote(env.voter, 'upvote', ':wave:') is None
        assert counts(env.reply) == (1, 0, 1)


class TestTwoPeopleVoting:
    def test_their_votes_add_up(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.baseline.user4, 'upvote', None)
        assert counts(env.post) == (2, 0, 2)

    def test_one_each_way(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.baseline.user4, 'downvote', None)
        assert counts(env.post) == (1, 1, 0)

    def test_one_of_them_changing_their_mind(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.baseline.user4, 'upvote', None)
        env.post.vote(env.voter, 'downvote', None)
        assert counts(env.post) == (1, 1, 0)


class TestWhatAReversalDoesToReputation:
    """D1302. The score moved by two and the reputation by one."""

    def test_an_upvote_turned_into_a_downvote(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_a_downvote_turned_into_an_upvote(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.post.vote(env.voter, 'downvote', None)
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 11

    def test_the_order_of_the_clicks_does_not_matter(self, env):
        """The property the defect broke: reputation follows the votes that
        stand, not the path taken to them."""
        env.author.reputation = 0
        db.session.commit()
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'downvote', None)
        switched = reputation_of(env.author.id)
        env.post.vote(env.voter, 'reversal', None)
        env.post.vote(env.baseline.user4, 'downvote', None)
        assert reputation_of(env.author.id) == switched

    def test_the_same_on_a_comment(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.reply.vote(env.voter, 'upvote', None)
        env.reply.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_a_comment_downvote_turned_into_an_upvote(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.reply.vote(env.voter, 'downvote', None)
        env.reply.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 11

    def test_withdrawing_a_downvote_on_a_comment_gives_it_back(self, env):
        env.author.reputation = 10
        db.session.commit()
        env.reply.vote(env.voter, 'downvote', None)
        env.reply.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 10


class TestReputationInALowQualityCommunity:
    """The flag's own label: "upvotes in here don't add to reputation"."""

    @pytest.fixture(autouse=True)
    def low_quality(self, env):
        env.community.low_quality = True
        env.author.reputation = 10
        db.session.commit()

    def test_a_downvote_on_a_post_still_costs(self, env):
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_and_taking_it_back_gives_it_back(self, env):
        """D1303. The whole update was skipped here, so the cost stood for
        good -- an author could be pushed down by downvotes that no longer
        existed."""
        env.post.vote(env.voter, 'downvote', None)
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 10

    def test_turning_that_downvote_into_an_upvote(self, env):
        env.post.vote(env.voter, 'downvote', None)
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10

    def test_withdrawing_an_upvote_costs_nothing(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10

    def test_turning_an_upvote_into_a_downvote_costs_one(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.post.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_a_comment_upvote_earns_nothing_either(self, env):
        """D1305. `PostReply.vote` never read the flag."""
        env.reply.vote(env.voter, 'upvote', None)
        assert reputation_of(env.author.id) == 10

    def test_a_comment_downvote_still_costs(self, env):
        env.reply.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 9

    def test_and_taking_that_one_back_gives_it_back(self, env):
        env.reply.vote(env.voter, 'downvote', None)
        env.reply.vote(env.voter, 'downvote', None)
        assert reputation_of(env.author.id) == 10

    def test_the_votes_themselves_are_unaffected(self, env):
        env.post.vote(env.voter, 'upvote', None)
        env.reply.vote(env.voter, 'upvote', None)
        assert counts(env.post) == (1, 0, 1)
        assert counts(env.reply) == (1, 0, 1)


class TestTheRuleOnItsOwn:
    """`reputation_delta` is the one statement of the policy."""

    def test_an_upvote_earns_one(self):
        from app.models import reputation_delta
        assert reputation_delta(0, 1, False) == 1

    def test_a_downvote_costs_one(self):
        from app.models import reputation_delta
        assert reputation_delta(0, -1, False) == -1

    def test_an_upvote_in_a_low_quality_community_earns_nothing(self):
        from app.models import reputation_delta
        assert reputation_delta(0, 1, True) == 0

    def test_a_downvote_there_still_costs(self):
        from app.models import reputation_delta
        assert reputation_delta(0, -1, True) == -1

    def test_a_reversal_applies_both_halves(self):
        from app.models import reputation_delta
        assert reputation_delta(1, -1, False) == -2
        assert reputation_delta(-1, 1, False) == 2

    def test_a_reversal_there_applies_the_half_that_counts(self):
        from app.models import reputation_delta
        assert reputation_delta(1, -1, True) == -1
        assert reputation_delta(-1, 1, True) == 1

    def test_withdrawing_gives_back_what_was_given(self):
        from app.models import reputation_delta
        for low_quality in (False, True):
            for effect in (1, -1):
                assert reputation_delta(0, effect, low_quality) + \
                    reputation_delta(effect, 0, low_quality) == 0


class TestWhoMayDownvoteAComment:
    """D1304. The refusal `Post.vote` has always had."""

    def test_an_author_who_has_blocked_the_voter(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        assert env.reply.vote(env.voter, 'downvote', None) is None
        assert counts(env.reply) == (0, 0, 0)
        assert PostReplyVote.query.count() == 0

    def test_an_author_who_has_blocked_their_instance(self, env):
        from app.models import InstanceBlock
        db.session.add(InstanceBlock(user_id=env.author.id,
                                     instance_id=env.voter.instance_id))
        db.session.commit()
        assert env.reply.vote(env.voter, 'downvote', None) is None
        assert counts(env.reply) == (0, 0, 0)

    def test_that_voter_may_still_upvote(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        env.reply.vote(env.voter, 'upvote', None)
        assert counts(env.reply) == (1, 0, 1)

    def test_and_may_still_take_that_upvote_back(self, env):
        """A reversal is spelled 'reversal' at the refusal, so withdrawing a
        vote is never blocked -- which is what `Post.vote` does too."""
        env.reply.vote(env.voter, 'upvote', None)
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.voter.id))
        db.session.commit()
        assert env.reply.vote(env.voter, 'reversal', None) == 'Like'
        assert counts(env.reply) == (0, 0, 0)

    def test_somebody_the_author_has_not_blocked(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.baseline.user4.id))
        db.session.commit()
        env.reply.vote(env.voter, 'downvote', None)
        assert counts(env.reply) == (0, 1, -1)


class TestTheWebVotingGates:
    """D1306. `vote_for_reply` gated its API path and not its web path.

    `vote_for_post` gates both, so every one of these cases was refused for a
    post and accepted for a comment. The pair is asserted together here, since
    what makes this a defect is that the two disagree.
    """

    @pytest.fixture
    def web(self, env, app):
        from types import SimpleNamespace

        from flask import session as flask_session
        from flask_wtf.csrf import generate_csrf

        # `validation_required` and `approval_required` sit outside the vote
        # routes, and an unverified account never reaches the gate under test.
        env.voter.verified = True
        env.voter.private_key = 'x'
        db.session.commit()
        with app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(env.voter.id)
            session['_fresh'] = True
            session['csrf_token'] = raw
        return SimpleNamespace(client=client, token=token)

    def vote(self, web, path):
        return web.client.post(path, data={'csrf_token': web.token})

    def on_comment(self, web, env, direction):
        return self.vote(web, f'/comment/{env.reply.id}/{direction}/public')

    def on_post(self, web, env, direction):
        return self.vote(web, f'/post/{env.post.id}/{direction}/public')

    def ban_from_community(self, env):
        from app import cache
        from app.models import CommunityBan
        from app.utils import communities_banned_from
        db.session.add(CommunityBan(user_id=env.voter.id,
                                    community_id=env.community.id))
        db.session.commit()
        cache.delete_memoized(communities_banned_from, env.voter.id)

    def test_a_community_that_accepts_no_downvotes_refuses_a_comment(self, env, web):
        from app.constants import DOWNVOTE_ACCEPT_NONE
        env.community.downvote_accept_mode = DOWNVOTE_ACCEPT_NONE
        db.session.commit()
        assert self.on_comment(web, env, 'downvote').status_code == 200
        assert counts(env.reply) == (0, 0, 0)

    def test_and_refuses_a_post_the_same_way(self, env, web):
        from app.constants import DOWNVOTE_ACCEPT_NONE
        env.community.downvote_accept_mode = DOWNVOTE_ACCEPT_NONE
        db.session.commit()
        assert self.on_post(web, env, 'downvote').status_code == 200
        assert counts(env.post) == (0, 0, 0)

    def test_it_still_accepts_an_upvote_on_a_comment(self, env, web):
        from app.constants import DOWNVOTE_ACCEPT_NONE
        env.community.downvote_accept_mode = DOWNVOTE_ACCEPT_NONE
        db.session.commit()
        assert self.on_comment(web, env, 'upvote').status_code == 200
        assert counts(env.reply) == (1, 0, 1)

    def test_a_voter_the_community_has_banned_may_not_vote_on_a_comment(self, env, web):
        self.ban_from_community(env)
        assert self.on_comment(web, env, 'upvote').status_code == 200
        assert counts(env.reply) == (0, 0, 0)

    def test_nor_on_a_post(self, env, web):
        self.ban_from_community(env)
        assert self.on_post(web, env, 'upvote').status_code == 200
        assert counts(env.post) == (0, 0, 0)

    def test_nor_downvote_a_comment(self, env, web):
        self.ban_from_community(env)
        assert self.on_comment(web, env, 'downvote').status_code == 200
        assert counts(env.reply) == (0, 0, 0)

    def test_an_emoji_reaction_is_gated_too(self, env, web):
        """It is the same function, reached through another route."""
        self.ban_from_community(env)
        response = web.client.post(
            f'/comment/{env.reply.id}/upvote/public/emoji',
            data={'csrf_token': web.token, 'emoji': ':tada:'})
        assert response.status_code in (200, 302)
        assert counts(env.reply) == (0, 0, 0)

    def test_a_voter_whose_reputation_is_spent_may_not_downvote(self, env, web):
        """`can_downvote` refuses below -10, and the web comment path never
        asked."""
        env.voter.reputation = -11
        db.session.commit()
        assert self.on_comment(web, env, 'downvote').status_code == 200
        assert counts(env.reply) == (0, 0, 0)

    def test_that_voter_may_still_upvote(self, env, web):
        env.voter.reputation = -11
        db.session.commit()
        assert self.on_comment(web, env, 'upvote').status_code == 200
        assert counts(env.reply) == (1, 0, 1)

    def test_an_ordinary_member_votes_on_a_comment(self, env, web):
        assert self.on_comment(web, env, 'upvote').status_code == 200
        assert counts(env.reply) == (1, 0, 1)

    def test_and_on_a_post(self, env, web):
        assert self.on_post(web, env, 'upvote').status_code == 200
        assert counts(env.post) == (1, 0, 1)

    def test_an_ordinary_member_downvotes_a_comment(self, env, web):
        assert self.on_comment(web, env, 'downvote').status_code == 200
        assert counts(env.reply) == (0, 1, -1)

    def test_a_banned_user_is_still_refused_with_403(self, env, web):
        """The gate sits below the ban refusal, so a banned user gets 403 and
        not the buttons back unchanged -- `can_upvote` is false for them too,
        and the order is what decides which answer they get."""
        env.voter.banned = True
        db.session.commit()
        assert self.on_comment(web, env, 'upvote').status_code == 403
        assert counts(env.reply) == (0, 0, 0)

    def test_a_banned_user_voting_on_a_post_gets_the_buttons_back(self, env, web):
        """Where the two paths still differ, and harmlessly: `vote_for_post`
        has its gate ABOVE the ban refusal, so a banned user is answered with
        the buttons unchanged instead of 403. Either way no vote is cast --
        `can_upvote` is false for a banned user -- and 403 is only reached on
        the post path by a 'reversal', which
        tests/test_shared_post_interactions.py pins."""
        env.voter.banned = True
        db.session.commit()
        assert self.on_post(web, env, 'upvote').status_code == 200
        assert counts(env.post) == (0, 0, 0)
