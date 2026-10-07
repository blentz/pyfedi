"""Round 252: the ranking math, the YouTube embed, and four display helpers.

Four clusters in `app/models.py`, all small and all consequential.

    post_ranking          the 'hot' sort. Two guards -- a null date and a null score -- and the
                          log/sign arithmetic that orders every front page.
    Post.vote             the early-downvote amplifiers, `SPICY_UNDER_30` and
                          `SPICY_UNDER_60`, which decide how much a downvote moves a new post.
    youtube_embed         the timestamp and `rel` parameters carried into the embed URL,
                          including the `/shorts/` shape.
    PollChoice.percentage, ModLog.action_to_str / get_correct_link -- three one-liners rendered
                          on pages a reader sees.

The scoring lines matter because they are what makes a downvote on a new post count for more
than one on an old one: a mistake there is a front page ordered differently for everybody, and
nothing else would notice.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import ModLog, Poll, PollChoice, Post, PostVote, Site, utcnow
from tests.factories import (make_community, make_community_member, make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('rankland')
    author = make_user(api_baseline.instance_local, 'rankauthor', local=True)
    voter = make_user(api_baseline.instance_local, 'rankvoter', local=True)
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/r/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author, voter=voter,
                           post=post, baseline=api_baseline)


# --------------------------------------------------------------------------
# The 'hot' ranking
# --------------------------------------------------------------------------


class TestTheHotRanking:
    """`post_ranking` is `sign * log10(|score|) + seconds/45000`: a post's score moves it by a
    LOGARITHM while its age moves it linearly, which is what makes a new post with two votes
    outrank an old one with fifty.
    """

    def test_a_higher_score_ranks_higher_at_the_same_moment(self, env):
        now = utcnow()

        assert env.post.post_ranking(100, now) > env.post.post_ranking(10, now)

    def test_a_newer_post_ranks_higher_at_the_same_score(self, env):
        now = utcnow()

        assert env.post.post_ranking(10, now) > \
            env.post.post_ranking(10, now - timedelta(days=1))

    def test_a_negative_score_ranks_below_a_positive_one(self, env):
        """`sign` is -1 for a negative score, so the log term is subtracted rather than
        added -- a downvoted post sinks rather than rising by the magnitude of its score."""
        now = utcnow()

        assert env.post.post_ranking(-10, now) < env.post.post_ranking(10, now)

    def test_a_score_of_zero_is_ranked_by_age_alone(self, env):
        """`sign` is 0, so the log term drops out entirely. Two unvoted posts are ordered by
        when they were posted, which is what a new community's front page is."""
        now = utcnow()

        assert env.post.post_ranking(0, now) == pytest.approx(
            env.post.post_ranking(0, now), abs=1e-9)
        assert env.post.post_ranking(0, now) > \
            env.post.post_ranking(0, now - timedelta(hours=1))

    def test_a_null_score_is_treated_as_one(self, env):
        """`if score is None: score = 1`. The column is nullable, and `abs(None)` is a
        TypeError inside the sort of every listing."""
        now = utcnow()

        assert env.post.post_ranking(None, now) == env.post.post_ranking(1, now)

    def test_a_null_date_is_treated_as_now(self, env):
        """`if post_date is None: post_date = utcnow()`. A post whose date is missing ranks as
        brand new rather than raising -- `None - datetime` is a TypeError."""
        ranking = env.post.post_ranking(10, None)

        assert ranking == pytest.approx(env.post.post_ranking(10, utcnow()), abs=1e-3)

    def test_the_epoch_seconds_helper_counts_from_the_stored_epoch(self, env):
        """`epoch_seconds` is the other half, and it includes microseconds -- two posts a
        fraction of a second apart do not tie."""
        moment = env.post.epoch + timedelta(days=1, seconds=2, microseconds=500000)

        assert env.post.epoch_seconds(moment) == pytest.approx(86402.5)


# --------------------------------------------------------------------------
# What a downvote costs a new post
# --------------------------------------------------------------------------


class TestWhatADownvoteCostsANewPost:
    """`Post.vote` amplifies an early downvote: under 30 total votes by `SPICY_UNDER_30`, under
    60 by `SPICY_UNDER_60`. The amplified value goes into `score`, which the 'hot' sort reads,
    while `down_votes` keeps the true count -- so the two must not be confused.
    """

    @pytest.fixture(autouse=True)
    def amplifiers(self, env, monkeypatch):
        """Both multipliers DEFAULT TO 1.0 in `config.py`, which makes the amplification an
        identity -- no row could tell the three bands apart. They are set to distinct values
        here so each band's arithmetic is visible."""
        monkeypatch.setitem(env.app.config, 'SPICY_UNDER_30', 3.0)
        monkeypatch.setitem(env.app.config, 'SPICY_UNDER_60', 2.0)
        return env

    def _vote(self, env, direction='downvote'):
        env.post.vote(env.voter, direction, None)
        db.session.commit()
        db.session.refresh(env.post)

    def test_a_downvote_on_a_new_post_is_amplified(self, env):
        before = env.post.score or 0

        self._vote(env)

        assert env.post.down_votes == 1
        assert env.post.score == before - env.app.config['SPICY_UNDER_30']

    def test_a_post_past_thirty_votes_uses_the_second_multiplier(self, env):
        """The `elif`. The two bands are different numbers, so a row that only drove the
        first would pass against an implementation that used it everywhere."""
        env.post.up_votes = 40
        env.post.down_votes = 0
        env.post.score = 40
        db.session.commit()

        self._vote(env)

        assert env.post.score == 40 - env.app.config['SPICY_UNDER_60']

    def test_a_post_past_sixty_votes_is_not_amplified_at_all(self, env):
        """Neither band applies, so a downvote costs exactly one point -- an established
        thread is ranked on its real score."""
        env.post.up_votes = 70
        env.post.down_votes = 0
        env.post.score = 70
        db.session.commit()

        self._vote(env)

        assert env.post.score == 69

    def test_the_vote_row_records_the_true_effect_not_the_amplified_one(self, env):
        """`effect=-1.0` while `spicy_effect` is what reaches `score`. The row is what a
        later Undo reverses, so storing the amplified value would take the wrong amount back
        off."""
        self._vote(env)

        vote = PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).one()
        assert vote.effect == -1.0


# --------------------------------------------------------------------------
# The YouTube embed URL
# --------------------------------------------------------------------------


class TestTheYoutubeEmbedUrl:
    """`youtube_embed` turns a watch URL into the id and query the embed player needs. A
    timestamp arrives as `t` and the player wants `start`; `rel=0` asks YouTube not to suggest
    other channels' videos afterwards.
    """

    @staticmethod
    def _query(embed):
        from urllib.parse import parse_qs

        return parse_qs(embed.split('?', 1)[1]) if '?' in embed else {}

    def _embed(self, env, url, rel=False):
        env.post.url = url
        db.session.commit()
        return env.post.youtube_embed(rel=rel) if rel else env.post.youtube_embed()

    def test_a_watch_url_gives_the_id(self, env):
        assert self._embed(env, 'https://www.youtube.com/watch?v=abc123').startswith(
            'abc123')

    def test_a_timestamp_becomes_start(self, env):
        """D1424. The iframe player ignores `t` and honours `start`, so without the rename
        every timestamped link embedded from the beginning -- and the `/shorts/` branch below
        had the rename while this one did not.

        `'t=90' not in embed` would be satisfied by `start=90` itself, so the assertion is on
        the parsed query.
        """
        embed = self._embed(env, 'https://www.youtube.com/watch?v=abc123&t=90')

        # `rel` defaults to True on the method, so `rel=0` is in every answer.
        assert self._query(embed) == {'start': ['90'], 'rel': ['0']}

    def test_a_shorts_url_gives_the_id_too(self, env):
        """The other URL shape YouTube publishes. The id is in the PATH rather than the
        query, so it is a separate branch."""
        assert self._embed(
            env, 'https://www.youtube.com/shorts/xyz789').startswith('xyz789')

    def test_a_shorts_timestamp_is_renamed_as_well(self, env):
        """The same rename, in the second branch -- two copies is two places to forget it."""
        embed = self._embed(env, 'https://www.youtube.com/shorts/xyz789?t=45')

        assert self._query(embed) == {'start': ['45'], 'rel': ['0']}

    def test_rel_zero_is_added_when_asked_for(self, env):
        embed = self._embed(env, 'https://www.youtube.com/shorts/xyz789', rel=True)

        assert 'rel=0' in embed

    def test_a_url_that_is_not_youtube_embeds_nothing(self, env):
        assert self._embed(env, 'https://example.com/video') == ''


# --------------------------------------------------------------------------
# Three helpers a reader sees
# --------------------------------------------------------------------------


class TestAPollChoicesPercentage:

    def test_the_share_is_floored(self, env):
        """`math.floor`, so the bars never total more than 100 -- three choices with one vote
        each are 33% each, not 34%."""
        choice = PollChoice(post_id=env.post.id, choice_text='one', sort_order=1,
                            num_votes=1)
        db.session.add(choice)
        db.session.commit()

        assert choice.percentage(3) == 33

    def test_every_vote_on_one_choice_is_a_hundred(self, env):
        choice = PollChoice(post_id=env.post.id, choice_text='one', sort_order=1,
                            num_votes=7)
        db.session.add(choice)
        db.session.commit()

        assert choice.percentage(7) == 100

    def test_a_choice_nobody_picked_is_zero(self, env):
        choice = PollChoice(post_id=env.post.id, choice_text='one', sort_order=1,
                            num_votes=0)
        db.session.add(choice)
        db.session.commit()

        assert choice.percentage(5) == 0


class TestHowTheModlogDescribesItself:

    def _entry(self, env, action, link='post/1'):
        entry = ModLog(user_id=env.author.id, type='mod', action=action, public=True,
                       link=link, link_text='a thing')
        db.session.add(entry)
        db.session.commit()
        return entry

    def test_a_known_action_is_translated(self, env):
        assert self._entry(env, 'delete_post').action_to_str() != 'delete_post'

    def test_an_unknown_action_is_shown_as_it_was_stored(self, env):
        """The `else`. The modlog is public, and an action added to the code without a label
        would otherwise render as nothing at all -- the raw name is worse than a translation
        and better than a blank."""
        assert self._entry(env, 'some_future_action').action_to_str() == \
            'some_future_action'

    @pytest.mark.parametrize('action', ['add_mod', 'remove_mod', 'delete_user',
                                        'undelete_user', 'ban_user'])
    def test_a_user_action_link_is_prefixed(self, env, action):
        """These actions target a person, and their `link` is stored as a bare username -- so
        the modlog has to build `u/<name>` or every one of those rows links to a post path
        that does not exist."""
        assert self._entry(env, action, link='someone').get_correct_link() == 'u/someone'

    def test_a_link_that_already_names_a_user_is_left_alone(self, env):
        """`and not self.link.startswith("u/")`. Some producers already write the prefix, and
        `u/u/someone` is a 404."""
        assert self._entry(env, 'ban_user', link='u/someone').get_correct_link() == \
            'u/someone'

    def test_a_post_action_link_is_left_alone(self, env):
        assert self._entry(env, 'delete_post', link='post/1').get_correct_link() == \
            'post/1'


# --------------------------------------------------------------------------
# A vote keeps a post ranked by when it was published
# --------------------------------------------------------------------------


def test_a_vote_ranks_a_backfilled_post_by_when_it_was_published(env):
    """`created_at` is when the content arrived here; `posted_at` is when its server published it. A backfilled
    post arrives days after it was published, and the hot sort reads `ranking`, so a vote that re-ranked it from
    `created_at` moved an old video back to the top of the media tab (interop D24)."""
    from datetime import timedelta
    env.post.posted_at = utcnow() - timedelta(days=30)
    env.post.created_at = utcnow()
    db.session.commit()

    env.post.vote(env.voter, 'upvote', None)
    db.session.commit()
    db.session.refresh(env.post)

    assert env.post.ranking == env.post.post_ranking(env.post.score + env.post.reply_count, env.post.posted_at)
    assert env.post.ranking < env.post.post_ranking(env.post.score + env.post.reply_count, env.post.created_at)
