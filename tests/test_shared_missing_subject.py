"""What the shared post and reply actions do with an id nobody holds.

Every function in `app/shared/post.py` and `app/shared/reply.py` starts by resolving
the post or reply it is about. They are called from web routes (`src=SRC_WEB`) and from
the API (`src=SRC_API`), and the two contexts reach them differently: a web route may
pass an id straight out of the URL, while `app/api/alpha/utils/*.py` usually resolves
it first with `a_post()` or `a_reply()`, which raise.

D1368. The same question was answered four ways in those two files:

    reply.py:21   db.session.query(PostReply).filter_by(id=reply_id).one()  NoResultFound
    reply.py:42   db.session.get(PostReply, reply_id) or abort(404)          404
    post.py:101   if db.session.get(Post, post_id) is None: ...              explicit
    fifteen more  db.session.get(Post, post_id)   then post.<attribute>      AttributeError

The fourth group is the defect: a bare `get` followed by a dereference. `vote_for_post`
had it on its API branch while `vote_for_reply` -- its twin, four lines apart in the
other file -- used `.one()`. Most of those fifteen are not reachable today, because the
API callers resolve the id upstream and two of the web routes carry `or abort(404)`
themselves; they were a 500 waiting for the first caller that did not.

All fifteen now read `db.session.get(...) or abort(404)`, which is what two of the three
guarded sites in the same files already did. For a guarded caller nothing changes; for
an unguarded one a 500 becomes a 404.
"""
import pytest
from flask import g
from werkzeug.exceptions import NotFound

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Site

MISSING = 999999


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    import app.shared.post as shared_post
    import app.shared.reply as shared_reply

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    # these functions read `current_user` on their SRC_WEB branch
    monkeypatch.setattr(shared_post, 'current_user', api_baseline.user1,
                        raising=False)
    monkeypatch.setattr(shared_reply, 'current_user', api_baseline.user1,
                        raising=False)
    return SimpleNamespace(baseline=api_baseline, post=shared_post,
                           reply=shared_reply)


class TestThePostActions:
    """Each is called with an id nobody holds, on the branch a web route uses."""

    def test_vote_for_post(self, env):
        with pytest.raises(NotFound):
            env.post.vote_for_post(MISSING, 'upvote', True, None, SRC_WEB)

    def test_delete_post(self, env):
        with pytest.raises(NotFound):
            env.post.delete_post(MISSING, False, SRC_WEB, None)

    def test_restore_post(self, env):
        with pytest.raises(NotFound):
            env.post.restore_post(MISSING, SRC_WEB, None)

    def test_lock_post(self, env):
        with pytest.raises(NotFound):
            env.post.lock_post(MISSING, True, SRC_WEB, None)

    def test_sticky_post(self, env):
        with pytest.raises(NotFound):
            env.post.sticky_post(MISSING, True, SRC_WEB, None)

    def test_hide_post(self, env):
        with pytest.raises(NotFound):
            env.post.hide_post(MISSING, True, SRC_WEB, None)

    def test_move_post(self, env):
        with pytest.raises(NotFound):
            env.post.move_post(MISSING, env.baseline.community1.id, SRC_WEB, None)

    def test_mod_remove_post(self, env):
        with pytest.raises(NotFound):
            env.post.mod_remove_post(MISSING, 'spam', SRC_WEB, None)

    def test_mod_restore_post(self, env):
        with pytest.raises(NotFound):
            env.post.mod_restore_post(MISSING, 'mistake', SRC_WEB, None)

    def test_vote_for_post_on_the_api_branch(self, env, monkeypatch):
        """The branch the twin got right with `.one()`. `authorise_api_user` is
        reached only after the post is resolved, so the guard has to come first --
        which is why this passes an auth value that would not survive it."""
        with pytest.raises(NotFound):
            env.post.vote_for_post(MISSING, 'upvote', True, None, SRC_API,
                                   'Bearer nonsense')


class TestTheReplyActions:
    def test_lock_post_reply(self, env):
        with pytest.raises(NotFound):
            env.reply.lock_post_reply(MISSING, True, SRC_WEB, None)

    def test_set_collapse_post_reply(self, env):
        with pytest.raises(NotFound):
            env.reply.set_collapse_post_reply(MISSING, True, SRC_WEB, None)

    def test_choose_answer(self, env):
        """Round 161 gave the ROUTE its `or abort(404)`; this is the shared function
        the route calls, which had none of its own."""
        with pytest.raises(NotFound):
            env.reply.choose_answer(MISSING, SRC_WEB)

    def test_unchoose_answer(self, env):
        with pytest.raises(NotFound):
            env.reply.unchoose_answer(MISSING, SRC_WEB)

    def test_vote_for_reply_on_the_api_branch(self, env):
        """Unchanged: this one already used `.one()`, so it raises NoResultFound
        rather than NotFound. Asserted so the difference is deliberate rather than
        something a later sweep flattens by accident."""
        from sqlalchemy.orm.exc import NoResultFound

        with pytest.raises(NoResultFound):
            env.reply.vote_for_reply(MISSING, 'upvote', True, None, SRC_API,
                                     'Bearer nonsense')


class TestARealSubjectStillWorks:
    """The guards are on the missing-row path; the ordinary path must be untouched."""

    def test_hiding_a_post_that_exists(self, env):
        from app.models import Post

        post = db.session.get(Post, env.baseline.post1.id)

        env.post.hide_post(post.id, True, SRC_WEB, None)

        # hide_post writes a row in `hidden_posts` rather than on the Post
        assert db.session.execute(
            db.text('SELECT count(*) FROM hidden_posts WHERE hidden_post_id = :id '
                    'AND user_id = :user'),
            {'id': post.id, 'user': env.baseline.user1.id}).scalar() == 1

    def test_collapsing_a_reply_that_exists(self, app, env):
        """`flash()` on the SRC_WEB path needs a request context, which is why this
        one is wrapped and the missing-row tests above are not: `abort(404)` is
        raised before `flash` is reached."""
        from app.models import PostReply

        reply = db.session.get(PostReply, env.baseline.reply1.id)
        reply.community.user_id = env.baseline.user1.id   # makes user1 the owner
        db.session.commit()

        with app.test_request_context():
            env.reply.set_collapse_post_reply(reply.id, True, SRC_WEB, None)

        db.session.refresh(reply)
        assert reply.collapsible is True


class TestThePropertyForBothFiles:
    """A sixteenth site appearing without a guard is what this catches."""

    def test_every_resolved_post_or_reply_is_guarded(self):
        import re
        from pathlib import Path

        bare = re.compile(r'= db\.session\.get\((?:Post|PostReply), '
                          r'(?:post_id|reply_id|post_reply_id)\)\s*$')
        offenders = []
        for name in ('app/shared/post.py', 'app/shared/reply.py'):
            for number, line in enumerate(
                    Path(name).read_text(encoding='utf8').splitlines(), 1):
                if bare.search(line):
                    offenders.append(f'{name}:{number}: {line.strip()}')

        assert offenders == [], (
            'these resolve a post or reply id and do not guard the result: '
            + '; '.join(offenders))
