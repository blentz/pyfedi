"""Round 254: finding the thing a peer's activity refers to.

Two helpers in `app/activitypub/util.py`, both given an `ap_id` a peer chose.

`find_reply_parent` decides what an incoming reply is a reply TO. It reads hints out of the URL
-- `comment` means a reply, `post` means a post -- and then, because those hints are only
conventions and NodeBB's comment URLs contain `/post/`, falls back to trying both lookups
outright. That fallback had no rows, and it is the arm every NodeBB reply takes.

`find_liked_object` resolves the target of a vote, and its answer decides where the vote lands.
It caches the id and type rather than the model, refuses a non-string id (an Undo's `object` is
whatever the peer put there), and answers None for an ARCHIVED post -- twice, once on each side
of the cache, because a post can be archived after the id was cached.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import cache, db
from app.activitypub.util import (find_liked_object, find_reply_parent,
                                  _find_liked_object_id)
from app.models import Post, PostReply, Site
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_user)


@pytest.fixture
def real_cache(app, monkeypatch):
    """`_find_liked_object_id` is memoized. With `CACHE_TYPE = 'NullCache'` every call is a
    miss, so a row about the cached path has to install a real backend."""
    from cachelib import SimpleCache

    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    return cache


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('replyland')
    author = make_user(api_baseline.instance_local, 'replyauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://peer.example/post/1')
    db.session.commit()
    reply = make_post_reply(post, author, body='the parent reply')
    reply.ap_id = 'https://peer.example/comment/1'
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author, post=post,
                           reply=reply, baseline=api_baseline)


# --------------------------------------------------------------------------
# What a reply is a reply to
# --------------------------------------------------------------------------


class TestWhatAnIncomingReplyRepliesTo:
    """The answer is `(post_id, parent_comment_id, root_id)`. A reply to a POST has no parent
    comment; a reply to a COMMENT carries the comment and the thread root, which is what keeps
    a deep thread threaded.

    THE TWO HINT BRANCHES ARE REDUNDANT, and the mutation pass says so: deleting either the
    `'comment' in in_reply_to` branch or the `'post' in in_reply_to` one changes no answer,
    because the fallback below tries both lookups outright. They are an optimisation -- one
    query instead of two for a peer whose URLs follow the convention -- and the fallback is
    what actually resolves everything. Recorded rather than chased, since a row cannot
    distinguish them.
    """

    def test_a_comment_url_resolves_to_the_comment_and_its_post(self, env):
        post_id, parent_id, root_id = find_reply_parent(env.reply.ap_id)

        assert post_id == env.post.id
        assert parent_id == env.reply.id
        assert root_id == env.reply.root_id

    def test_a_reply_to_a_nested_comment_carries_the_thread_root(self, env):
        """`root_id` is what keeps a deep thread threaded: every reply below the first level
        records the top of its own branch rather than its immediate parent. A top-level
        comment's `root_id` is NULL, so a row asserting it would pass against a helper that
        dropped the value -- this one uses a nested comment, whose root is set.
        """
        nested = make_post_reply(env.post, env.author, body='a nested reply')
        nested.ap_id = 'https://peer.example/comment/2'
        nested.parent_id = env.reply.id
        nested.root_id = env.reply.id
        db.session.commit()

        post_id, parent_id, root_id = find_reply_parent(nested.ap_id)

        assert post_id == env.post.id
        assert parent_id == nested.id
        assert root_id == env.reply.id

    def test_the_fallback_carries_the_thread_root_too(self, env):
        """The same three values through the FALLBACK rather than through the hint branch --
        the hint branch is reached by a url containing `comment`, so a nested reply on a peer
        with a flat url scheme goes the other way, and that copy of the assignment needs its
        own row."""
        nested = make_post_reply(env.post, env.author, body='a nested reply')
        nested.ap_id = 'https://peer.example/objects/3'
        nested.parent_id = env.reply.id
        nested.root_id = env.reply.id
        db.session.commit()

        post_id, parent_id, root_id = find_reply_parent(nested.ap_id)

        assert post_id == env.post.id
        assert parent_id == nested.id
        assert root_id == env.reply.id

    def test_a_post_url_resolves_to_the_post_with_no_parent(self, env):
        post_id, parent_id, root_id = find_reply_parent(env.post.ap_id)

        assert post_id == env.post.id
        assert parent_id is None
        assert root_id is None

    def test_a_comment_url_the_hint_cannot_resolve_falls_back(self, env):
        """The fallback, and the case the comment in the source names: NodeBB's comment URLs
        contain `/post/`, so the `post` hint matches and the Post lookup misses -- and without
        the fallback the reply would be attached to nothing."""
        env.reply.ap_id = 'https://nodebb.example/post/1/2'
        db.session.commit()

        post_id, parent_id, root_id = find_reply_parent(env.reply.ap_id)

        assert parent_id == env.reply.id
        assert post_id == env.post.id

    def test_a_url_with_no_hint_at_all_is_tried_both_ways(self, env):
        """A peer whose URLs contain neither word. The fallback tries the comment lookup first
        and the post lookup second, so a url naming a POST still resolves."""
        env.post.ap_id = 'https://peer.example/x/1'
        db.session.commit()

        post_id, parent_id, _root = find_reply_parent('https://peer.example/x/1')

        assert post_id == env.post.id
        assert parent_id is None

    def test_a_url_naming_nothing_resolves_to_nothing(self, env):
        """All three lookups miss. Every caller tests the result, and the alternative would be
        an exception on an ordinary event: a reply whose parent this instance never received."""
        assert find_reply_parent('https://peer.example/comment/nothing') == \
            (None, None, None)


# --------------------------------------------------------------------------
# What a vote is a vote on
# --------------------------------------------------------------------------


class TestWhatAVoteIsAVoteOn:

    def test_a_comment_id_resolves_to_the_comment(self, env, real_cache):
        """The `'/comment/' in ap_id` branch is the same kind of optimisation as the reply
        parent's hints: deleting it still resolves the comment, through the `else` arm's own
        PostReply fallback. One query saved, no behaviour changed."""
        assert find_liked_object(env.reply.ap_id).id == env.reply.id

    def test_a_post_id_resolves_to_the_post(self, env, real_cache):
        assert find_liked_object(env.post.ap_id).id == env.post.id

    def test_a_post_id_that_looks_like_neither_is_still_found(self, env, real_cache):
        """`'/comment/' in ap_id` is the only hint, so everything else takes the `else` --
        which tries Post first and PostReply second."""
        env.post.ap_id = 'https://peer.example/objects/1'
        db.session.commit()
        cache.delete_memoized(_find_liked_object_id)

        assert find_liked_object('https://peer.example/objects/1').id == env.post.id

    def test_a_comment_whose_url_has_no_hint_is_found_by_the_second_lookup(self, env,
                                                                          real_cache):
        """The `else` branch's own fallback: a PostReply whose ap_id contains no `/comment/`.
        Both lookups in one branch, so a peer using a flat URL scheme still gets its votes
        counted."""
        env.reply.ap_id = 'https://peer.example/objects/2'
        db.session.commit()
        cache.delete_memoized(_find_liked_object_id)

        found = find_liked_object('https://peer.example/objects/2')

        assert isinstance(found, PostReply)
        assert found.id == env.reply.id

    def test_an_id_that_is_not_a_string_is_refused(self, env, real_cache):
        """The guard the comment in the source explains: the id comes from
        `core_activity['object']['object']` on an Undo, which is whatever the peer put there --
        including a dict and including nothing. `'/comment/' in None` is a TypeError."""
        assert find_liked_object(None) is None
        assert find_liked_object({'id': 'https://peer.example/post/1'}) is None
        assert find_liked_object(42) is None

    def test_an_archived_post_cannot_be_voted_on(self, env, real_cache):
        """An archived post's replies have been collapsed and its score is fixed, so a vote
        arriving afterwards has nowhere to land. The refusal happens on the CACHED path, which
        is the one a repeat vote takes."""
        find_liked_object(env.post.ap_id)  # prime the cache

        env.post.archived = True
        db.session.commit()

        assert find_liked_object(env.post.ap_id) is None

    def test_an_archived_post_is_refused_on_the_uncached_path_too(self, env, real_cache):
        """The same refusal inside `_find_liked_object_id`, which is where a FIRST vote on an
        already-archived post arrives.

        The two copies are REDUNDANT: deleting either one still answers None, because the other
        catches it. Both are kept because they are one query apart -- the inner one avoids
        caching an id that will always be rejected -- and the survivor is recorded rather than
        chased.
        """
        env.post.archived = True
        db.session.commit()
        cache.delete_memoized(_find_liked_object_id)

        assert find_liked_object(env.post.ap_id) is None

    def test_an_archived_comment_is_still_found(self, env, real_cache):
        """Asserted rather than assumed: only `Post` carries `archived`, and the PostReply arm
        has no such test -- so a comment on an archived post is still resolvable, which is what
        lets a vote on it be recorded."""
        env.reply.ap_id = 'https://peer.example/comment/1'
        db.session.commit()

        assert find_liked_object(env.reply.ap_id).id == env.reply.id

    def test_an_id_nobody_holds_resolves_to_nothing(self, env, real_cache):
        assert find_liked_object('https://peer.example/comment/nothing') is None

    def test_the_lookup_is_cached_by_id_and_type(self, env, real_cache):
        """`_find_liked_object_id` is memoized for 20 minutes and returns a
        `(id, type)` TUPLE rather than a model, because a SQLAlchemy object cannot be
        serialised into redis. The row proves the cache is read by deleting the row behind
        it: the id comes back from the cache, and the fresh lookup by primary key then finds
        nothing."""
        assert _find_liked_object_id(env.post.ap_id) == (env.post.id, 'Post')

        post_id = env.post.id
        db.session.query(PostReply).filter_by(post_id=post_id).delete()
        db.session.query(Post).filter_by(id=post_id).delete()
        db.session.commit()

        assert _find_liked_object_id(env.post.ap_id) == (post_id, 'Post')
        assert find_liked_object(env.post.ap_id) is None
