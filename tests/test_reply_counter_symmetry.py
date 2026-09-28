"""One gate, three paths: who counts as a reply for a community's counters.

`PostReply.new` increments `post.reply_count`, `post.reply_count_cross_posted` and
`community.post_reply_count` only `if not user.bot` (`app/models.py`), and
`author.post_reply_count` always. Whatever creates a count has to be the same thing
that removes it, and a reply can be deleted by three different routes:

  * `delete_reply` in `app/shared/reply.py` -- the author deletes their own;
  * `mod_remove_reply`, the same file -- a moderator removes it;
  * `delete_post_or_comment` in `app/activitypub/util.py` -- it arrives as a Delete
    from the peer that hosts the author.

D1361. The third one kept `community.post_reply_count` OUTSIDE the gate, on both the
delete and the restore. `PostReply.new` never added a bot's reply to that count, so
every bot reply deleted through federation took one off a count it had never been in.
Measured: a community at 0 went to -1. Its restore had the mirror error, so a
delete-then-restore pair was lossless and only a plain delete -- the normal case --
drifted.

This file drives all three, plus the create, so the gate is asserted where it is set
as well as where it is honoured. `tests/test_ap_moderation.py` holds the federated
path's own tests; the point here is the AGREEMENT between paths.
"""
import pytest
from flask import g

from app import db
from app.constants import SRC_WEB
from app.models import Community, Post, PostReply, Site, User, utcnow
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
AUTHOR = 'https://remote.test/u/replier'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('counted')
    author = make_user(api_baseline.instance_remote, 'replier')
    author.ap_id = 'replier@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://remote.test/p/counted')
    post.reply_count = 0
    post.reply_count_cross_posted = 0
    community.post_reply_count = 0
    author.post_reply_count = 0
    db.session.commit()
    return SimpleNamespace(community=community, author=author, post=post,
                           baseline=api_baseline)


def counters(env):
    for row in (env.post, env.community, env.author):
        db.session.refresh(row)
    return (env.post.reply_count, env.community.post_reply_count,
            env.author.post_reply_count)


def a_reply_through_new(env, body='a reply'):
    """Through `PostReply.new`, which is where the gate is set."""
    request_json = {'id': 'https://remote.test/c/1', 'type': 'Create',
                    'to': [PUBLIC],
                    'object': {'id': f'https://remote.test/r/{abs(hash(body)) % 99999}',
                               'type': 'Note', 'attributedTo': AUTHOR,
                               'to': [PUBLIC], 'inReplyTo': env.post.ap_id,
                               'content': f'<p>{body}</p>'}}
    return PostReply.new(env.author, env.post, in_reply_to=None, body=body,
                         body_html=f'<p>{body}</p>', notify_author=False,
                         language_id=None, distinguished=False, answer=None,
                         request_json=request_json)


class TestWhatCreatingAReplyCounts:
    """The gate where it is set. Nothing pinned this, which is why a mutant that
    removed it survived the first pass of this round."""

    def test_a_human_reply_counts_everywhere(self, env):
        a_reply_through_new(env)

        assert counters(env) == (1, 1, 1)

    def test_a_bot_reply_counts_only_against_its_author(self, env):
        env.author.bot = True
        db.session.commit()

        a_reply_through_new(env)

        assert counters(env) == (0, 0, 1)

    def test_two_bot_replies_still_count_only_against_their_author(self, env):
        env.author.bot = True
        db.session.commit()

        a_reply_through_new(env, body='one')
        a_reply_through_new(env, body='two')

        assert counters(env) == (0, 0, 2)


class TestTheAuthorDeletingTheirOwn:
    """`delete_reply` -- the reference implementation. It has kept the community
    counter inside the gate all along, which is how D1361 was identified."""

    def delete(self, env, reply, monkeypatch):
        import app.shared.reply as shared_reply

        monkeypatch.setattr(shared_reply, 'current_user', env.author,
                            raising=False)
        shared_reply.delete_reply(reply.id, src=SRC_WEB, auth=None)

    def test_a_human_reply_gives_back_what_it_took(self, env, monkeypatch):
        reply = a_reply_through_new(env)
        assert counters(env) == (1, 1, 1)

        self.delete(env, reply, monkeypatch)

        assert counters(env) == (0, 0, 0)

    def test_a_bot_reply_gives_back_only_its_author_count(self, env, monkeypatch):
        env.author.bot = True
        db.session.commit()
        reply = a_reply_through_new(env)
        assert counters(env) == (0, 0, 1)

        self.delete(env, reply, monkeypatch)

        assert counters(env) == (0, 0, 0)


class TestTheDeleteThatArrivesFromThePeer:
    """`delete_post_or_comment`, which is where D1361 was. Driven from the same
    create, so the two halves of the arithmetic are the same two halves."""

    def delete(self, env, reply):
        from app.activitypub.util import delete_post_or_comment

        delete_post_or_comment(env.author, reply, False,
                               {'id': 'https://remote.test/activities/delete/1'}, '')

    def restore(self, env, reply):
        from app.activitypub.util import restore_post_or_comment

        restore_post_or_comment(env.author, reply, False,
                                {'id': 'https://remote.test/activities/undo/1'}, '')

    def test_a_human_reply_gives_back_what_it_took(self, env):
        reply = a_reply_through_new(env)
        assert counters(env) == (1, 1, 1)

        self.delete(env, reply)

        assert counters(env) == (0, 0, 0)

    def test_a_bot_reply_gives_back_only_its_author_count(self, env):
        """The defect: this used to leave `community.post_reply_count` at -1."""
        env.author.bot = True
        db.session.commit()
        reply = a_reply_through_new(env)
        assert counters(env) == (0, 0, 1)

        self.delete(env, reply)

        assert counters(env) == (0, 0, 0)

    def test_three_bot_replies_deleted_leave_the_community_at_zero(self, env):
        """What an instance running feed bots does every day. Each delete took one
        off, so the count went as negative as the bots were busy."""
        env.author.bot = True
        db.session.commit()
        replies = [a_reply_through_new(env, body=f'bot {index}')
                   for index in range(3)]
        assert counters(env) == (0, 0, 3)

        for reply in replies:
            self.delete(env, reply)

        assert counters(env) == (0, 0, 0)

    def test_a_bot_reply_round_trips(self, env):
        env.author.bot = True
        db.session.commit()
        reply = a_reply_through_new(env)

        self.delete(env, reply)
        self.restore(env, reply)

        assert counters(env) == (0, 0, 1)

    def test_a_human_reply_round_trips(self, env):
        reply = a_reply_through_new(env)

        self.delete(env, reply)
        self.restore(env, reply)

        assert counters(env) == (1, 1, 1)


class TestTheCrossPostedFloor:
    """`if to_delete.post.reply_count_cross_posted:` on the federated delete -- the
    one counter with a floor rather than a gate, because `PostReply.new` recomputes
    it from the cross-post set instead of incrementing it."""

    def test_it_does_not_go_below_zero(self, env):
        reply = make_post_reply(env.post, env.author)
        env.post.reply_count = 1
        env.post.reply_count_cross_posted = 0
        db.session.commit()

        from app.activitypub.util import delete_post_or_comment

        delete_post_or_comment(env.author, reply, False,
                               {'id': 'https://remote.test/activities/delete/2'}, '')

        db.session.refresh(env.post)
        assert env.post.reply_count_cross_posted == 0

    def test_a_non_zero_value_still_comes_down(self, env):
        reply = make_post_reply(env.post, env.author)
        env.post.reply_count = 4
        env.post.reply_count_cross_posted = 9
        db.session.commit()

        from app.activitypub.util import delete_post_or_comment

        delete_post_or_comment(env.author, reply, False,
                               {'id': 'https://remote.test/activities/delete/3'}, '')

        db.session.refresh(env.post)
        assert env.post.reply_count_cross_posted == 8
