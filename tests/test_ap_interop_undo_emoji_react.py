"""Interop C1, item 3 (EmojiReact): an Undo of an EmojiReact takes the reaction back,
and every undo of a vote rebuilds the emoji_reactions cache.

Before this change the Undo dispatch had no EmojiReact arm: the activity fell through
to 'Unmatched activity' and the reaction stayed. And `undo_vote` deleted the vote row
without rebuilding `Post.emoji_reactions` / `PostReply.emoji_reactions`, so an Undo
Like of a vote that carried an emoji (a Like with `content`, which `process_upvote`
stores as the vote's emoji) left the reaction on the page.

An inbound EmojiReact is a vote row with an emoji (`process_upvote` ->
`Post.vote(user, 'upvote', emoji)`), one per user and target. Its Undo is therefore
`undo_vote` on that row, and only when the row still carries the emoji being undone:
a user who has since switched to another reaction keeps the new one.
"""
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog, Post, PostReply, PostReplyVote, PostVote
from tests.factories import (make_community, make_post, make_post_reply, make_post_reply_vote,
                             make_post_vote, resolvable_remote_author, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_inbox_dispatch_undo_content import undo_activity

PEER = 'peer.example'


@pytest.fixture
def scene(app, db_session, monkeypatch):
    """A remote community with a post and a reply, and two remote voters.

    The voters come from `resolvable_remote_author`, so the dispatcher's actor lookup is a
    pure database read. `announce_activity_to_followers` is recorded rather than run: the
    Undo arm relays an undone vote, and the relay is not what these tests are about."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    record_moderation(monkeypatch, 'announce_activity_to_followers')
    instance = seed_community_owner(PEER)
    author = resolvable_remote_author(instance, 'author')
    voter = resolvable_remote_author(instance, 'voter')
    other = resolvable_remote_author(instance, 'other')
    community = make_community(host=PEER)
    post = make_post(community, author, f'https://{PEER}/post/1')
    reply = make_post_reply(post, author)
    reply.ap_id = f'https://{PEER}/comment/1'
    db.session.commit()
    return SimpleNamespace(voter=voter, other=other, post=post, reply=reply)


def react(user, target, emoji):
    """The state `Post.vote(user, 'upvote', emoji)` leaves: one upvote row carrying the
    emoji, the counters moved, and the cache rebuilt."""
    if isinstance(target, Post):
        vote = make_post_vote(user, target, 1.0)
    else:
        vote = make_post_reply_vote(user, target, 1.0)
    vote.emoji = emoji
    target.up_votes += 1
    target.score += 1
    db.session.commit()
    target.update_reaction_cache()
    db.session.commit()


def tokens(model, target_id):
    db.session.expire_all()
    reactions = db.session.get(model, target_id).emoji_reactions
    return None if reactions is None else [reaction['token'] for reaction in reactions]


def test_an_undo_of_an_emoji_react_on_a_post_takes_the_reaction_back(scene):
    react(scene.voter, scene.post, '🎉')
    react(scene.other, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id, content='🎉'))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) == ['👍']
    assert db.session.get(Post, scene.post.id).up_votes == 1
    log = ActivityPubLog.query.one()
    assert (log.activity_type, log.result) == ('Undo Vote', 'success')


def test_an_undo_of_an_emoji_react_on_a_reply_takes_the_reaction_back(scene):
    react(scene.voter, scene.reply, '🎉')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.reply.ap_id, content='🎉'))

    assert PostReplyVote.query.filter_by(user_id=scene.voter.id, post_reply_id=scene.reply.id).count() == 0
    assert tokens(PostReply, scene.reply.id) == []


def test_an_undo_naming_another_emoji_keeps_the_current_reaction(scene):
    """The voter has switched from 🎉 to 👍 since; the late Undo of the 🎉 must not take
    the 👍 away."""
    react(scene.voter, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id, content='🎉'))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 1
    assert tokens(Post, scene.post.id) == ['👍']


def test_an_undo_of_an_emoji_react_without_content_takes_the_vote_back(scene):
    """No emoji named, nothing to compare: the user's vote on the target is the reaction."""
    react(scene.voter, scene.post, '🎉')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) == []


def test_an_undo_like_of_a_vote_with_an_emoji_rebuilds_the_cache(scene):
    """The stale-cache defect the spec names for Undo Like: the row went, the 🎉 stayed."""
    react(scene.voter, scene.post, '🎉')
    react(scene.other, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'Like', scene.post.ap_id))

    assert tokens(Post, scene.post.id) == ['👍']


def test_an_undo_like_of_a_vote_with_an_emoji_on_a_reply_rebuilds_the_cache(scene):
    react(scene.voter, scene.reply, '🎉')

    dispatch(undo_activity(scene.voter, 'Like', scene.reply.ap_id))

    assert tokens(PostReply, scene.reply.id) == []


def test_an_undo_like_of_a_plain_vote_leaves_the_cache_alone(scene):
    """A vote with no emoji never touched the cache, so its undo does not write one: a post
    nobody reacted to keeps `emoji_reactions` None rather than gaining []."""
    make_post_vote(scene.voter, scene.post, 1.0)

    dispatch(undo_activity(scene.voter, 'Like', scene.post.ap_id))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) is None
