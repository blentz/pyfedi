"""`admin_content`: the moderation listing of bad, spammy and deleted content.

48 statements, none of them executed by any test before this file -- the largest
wholly uncovered route in `app/admin/routes.py`.

D1382. `title` was assigned only inside the three `show` branches and then passed
to the template, so any other value was
`UnboundLocalError: cannot access local variable 'title'`, which Flask answers as
a 500. The chain compares exact strings, so `?show=TRASH` was enough. Measured:

    show='trash'    status=200
    show='spammy'   status=200
    show='deleted'  status=200
    show='bogus'    UnboundLocalError
    show=''         UnboundLocalError
    show='TRASH'    UnboundLocalError

`show` is also reflected into the four pagination `url_for` calls, so an
unrecognised value propagated into every link on the page.

WHAT THE THREE VIEWS ARE FOR, and why the rows below assert on which content
appears rather than only on the status code:

  trash    down_votes > 1 AND score < 10, worst score first
  spammy   score <= 0, and with `days` set, only posts by accounts created in
           that window -- the join on User is what makes that possible
  deleted  deleted == True, newest first, and this branch REBUILDS the query from
           `Post.query`, dropping the `status > POST_STATUS_REVIEWING` filter the
           other two inherit

That last difference is deliberate and is pinned as such: a deleted post still in
review is exactly what an admin looking at deleted content wants to see, and it is
the only view that shows it.
"""
from datetime import timedelta

import pytest
from flask import g

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Language, Site, utcnow
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def env(app, db_session):
    """An admin, a community, and one post/reply pair per view.

    User id 1 is an admin (fact 347), which is what `permission_required(
    'administer all communities')` needs.
    """
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    admin = make_user(local, 'theadmin', local=True)
    assert admin.id == 1
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(author, community)

    def a_post(title, **columns):
        post = make_post(community, author, f'https://test.piefed.local/p/{title}',
                         title=title)
        for column, value in columns.items():
            setattr(post, column, value)
        return post

    # Names share no prefix with each other, because every assertion below is a
    # substring search over the whole page (fact 777: 'DOWNVOTED' inside
    # 'DOWNVOTEDREPLY' made three of these pass for the wrong reason).
    downvoted = a_post('TRASHPOST', down_votes=5, score=-4)
    spammy = a_post('SPAMPOST', down_votes=0, score=0)
    deleted = a_post('GONEPOST', deleted=True, down_votes=0, score=5)
    ordinary = a_post('FINEPOST', down_votes=0, score=20)
    # A popular post that NO reply hangs off, so it can witness "this post is
    # hidden" without a shown reply's parent title putting it back on the page.
    # FINEPOST cannot do that job once it is the replies' parent.
    quiet = a_post('QUIETPOST', down_votes=0, score=20)
    # One row per OPERAND of the trash filter, so each can be shown to matter:
    # plenty of downvotes but a high score, which `Post.score < 10` excludes.
    a_post('ARGUEDPOST', down_votes=9, score=50)
    db.session.commit()

    # Both replies hang off FINEPOST, which no view lists as a post. A reply
    # teaser renders its PARENT post's title, so a reply on TRASHPOST would put
    # 'TRASHPOST' on the page whenever the reply was shown -- and every "the post
    # is hidden" row below would then be asserting against a title the replies
    # listing had leaked.
    downvoted_reply = make_post_reply(ordinary, author, body='TRASHCOMMENT')
    downvoted_reply.down_votes = 5
    downvoted_reply.score = -4
    deleted_reply = make_post_reply(ordinary, author, body='GONECOMMENT')
    deleted_reply.deleted = True
    # The reply filter's other operand: a poor score but no downvotes, which
    # `PostReply.down_votes > 1` excludes.
    unloved_reply = make_post_reply(ordinary, author, body='UNLOVEDCOMMENT')
    unloved_reply.down_votes = 0
    unloved_reply.score = -4
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    g.admin_ids = [admin.id]
    return client, downvoted, spammy, deleted, ordinary, quiet


def page(client, **params):
    return client.get('/admin/content', query_string=params)


# --------------------------------------------------------------------------
# D1382: the parameter that produced a 500
# --------------------------------------------------------------------------


class TestAnUnrecognisedShow:
    @pytest.mark.parametrize('show', ['bogus', '', 'TRASH', 'Deleted', 'trash ',
                                      '../../etc/passwd', '0'])
    def test_it_answers_rather_than_raising(self, app, env, show):
        client = env[0]

        assert page(client, show=show).status_code == 200

    @pytest.mark.parametrize('show', ['bogus', '', 'TRASH'])
    def test_it_falls_back_to_the_default_view(self, app, env, show):
        """`request.args.get('show', 'trash')` names `trash` as the default, so
        an unrecognised value behaves as `trash` rather than as some other view.
        Asserted by the content, not the status: the downvoted post is in the
        trash view and the deleted one is not."""
        client = env[0]

        body = page(client, show=show).data

        assert b'TRASHPOST' in body
        assert b'GONEPOST' not in body

    @pytest.mark.parametrize('show', ['bogus', 'TRASH'])
    def test_the_pagination_links_do_not_carry_it(self, app, env, show):
        """`show` is reflected into four `url_for` calls, so an unrecognised
        value used to propagate into every link on the page."""
        client = env[0]

        body = page(client, show=show).data

        assert show.encode() not in body or show == 'TRASH'


# --------------------------------------------------------------------------
# The three views
# --------------------------------------------------------------------------


class TestTheTrashView:
    def test_it_shows_downvoted_content(self, app, env):
        client = env[0]

        body = page(client, show='trash').data

        assert b'TRASHPOST' in body

    def test_it_hides_content_that_is_merely_unpopular(self, app, env):
        """`down_votes > 1, score < 10` -- both operands. A post with no
        downvotes and a low score is not trash."""
        client = env[0]

        body = page(client, show='trash').data

        assert b'SPAMPOST' not in body

    def test_it_hides_popular_content(self, app, env):
        client = env[0]

        assert b'QUIETPOST' not in page(client, show='trash').data

    def test_a_downvoted_post_with_a_high_score_is_not_trash(self, app, env):
        """`Post.score < 10`, the operand `down_votes > 1` alone does not cover:
        a contested but well-liked post has plenty of downvotes and a good
        score."""
        client = env[0]

        assert b'ARGUEDPOST' not in page(client, show='trash').data

    def test_a_low_scoring_reply_with_no_downvotes_is_not_trash(self, app, env):
        """`PostReply.down_votes > 1`, the operand `score < 10` alone does not
        cover."""
        client = env[0]

        assert b'UNLOVEDCOMMENT' not in page(client, show='trash').data

    def test_it_hides_deleted_content(self, app, env):
        """`Post.deleted == False` on the base query, which the deleted view
        replaces rather than extends."""
        client = env[0]

        assert b'GONEPOST' not in page(client, show='trash').data

    def test_the_replies_are_listed_too(self, app, env):
        client = env[0]

        assert b'TRASHCOMMENT' in page(client, show='trash').data


class TestTheSpammyView:
    def test_it_shows_content_with_no_score(self, app, env):
        client = env[0]

        assert b'SPAMPOST' in page(client, show='spammy', days=0).data

    def test_it_hides_content_with_a_score(self, app, env):
        client = env[0]

        assert b'QUIETPOST' not in page(client, show='spammy', days=0).data

    def test_days_restricts_it_to_new_accounts(self, app, env):
        """The `User.created` filter, which only this view applies and only when
        `days > 0` -- it is why the base query joins User. The author here was
        created just now, so a one-day window includes them and a window that
        has already closed does not."""
        client, downvoted, spammy, deleted, ordinary, quiet = env
        from app.models import User

        author = User.query.filter_by(user_name='author').one()
        author.created = utcnow() - timedelta(days=30)
        db.session.commit()

        assert b'SPAMPOST' not in page(client, show='spammy', days=3).data
        assert b'SPAMPOST' in page(client, show='spammy', days=0).data


class TestTheDeletedView:
    def test_it_shows_deleted_content(self, app, env):
        client = env[0]

        assert b'GONEPOST' in page(client, show='deleted', days=0).data

    def test_it_hides_content_that_is_not_deleted(self, app, env):
        client = env[0]

        body = page(client, show='deleted', days=0).data

        assert b'TRASHPOST' not in body
        assert b'QUIETPOST' not in body

    def test_it_shows_a_deleted_post_still_in_review(self, app, env):
        """This branch rebuilds the query from `Post.query`, dropping the
        `status > POST_STATUS_REVIEWING` filter the other two views inherit.
        Deliberate -- a deleted post in review is exactly what this view is for,
        and it is the only one that shows it -- so it is pinned rather than left
        to chance."""
        client, downvoted, spammy, deleted, ordinary, quiet = env
        deleted.status = POST_STATUS_REVIEWING
        db.session.commit()

        assert b'GONEPOST' in page(client, show='deleted', days=0).data

    def test_the_deleted_replies_are_listed(self, app, env):
        client = env[0]

        assert b'GONECOMMENT' in page(client, show='deleted', days=0).data


# --------------------------------------------------------------------------
# The other parameters
# --------------------------------------------------------------------------


class TestThePostsRepliesFilter:
    def test_posts_only_hides_the_replies(self, app, env):
        client = env[0]

        body = page(client, show='trash', posts_replies='posts').data

        assert b'TRASHPOST' in body
        assert b'TRASHCOMMENT' not in body

    def test_replies_only_hides_the_posts(self, app, env):
        client = env[0]

        body = page(client, show='trash', posts_replies='replies').data

        assert b'TRASHCOMMENT' in body
        assert b'TRASHPOST' not in body

    def test_an_unrecognised_value_shows_both(self, app, env):
        """Neither `== 'posts'` nor `== 'replies'`, so neither
        `filter(False)` runs -- the default, and the only arm with no explicit
        branch."""
        client = env[0]

        body = page(client, show='trash', posts_replies='nonsense').data

        assert b'TRASHPOST' in body and b'TRASHCOMMENT' in body


class TestDays:
    def test_zero_removes_the_time_window(self, app, env):
        """`if days > 0` guards every date filter, so 0 means "all time" rather
        than "nothing"."""
        client, downvoted, spammy, deleted, ordinary, quiet = env
        downvoted.posted_at = utcnow() - timedelta(days=365)
        db.session.commit()

        assert b'TRASHPOST' in page(client, show='trash', days=0).data

    def test_a_window_excludes_older_content(self, app, env):
        client, downvoted, spammy, deleted, ordinary, quiet = env
        downvoted.posted_at = utcnow() - timedelta(days=365)
        db.session.commit()

        assert b'TRASHPOST' not in page(client, show='trash', days=3).data

    def test_a_non_numeric_days_takes_the_default(self, app, env):
        """`type=int` makes Flask substitute the default rather than raise."""
        client = env[0]

        assert page(client, show='trash', days='abc').status_code == 200


class TestWhoMaySeeIt:
    def test_an_anonymous_visitor_may_not(self, app, env):
        response = app.test_client().get('/admin/content')

        assert response.status_code in (302, 401, 403)

    def test_an_ordinary_user_may_not(self, app, env):
        """`permission_required('administer all communities')`."""
        local = make_instance('other.piefed.local')
        ordinary = make_user(local, 'ordinaryperson', local=True)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(ordinary.id)
            sess['_fresh'] = True

        assert client.get('/admin/content').status_code in (302, 401, 403)
