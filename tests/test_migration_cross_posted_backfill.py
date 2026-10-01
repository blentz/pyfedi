"""The backfill for `post.reply_count_cross_posted` (D543).

`migrations/versions/6814385881d3_cross_posted_reply_count.py` added the
column as `nullable=True` with no server default and no backfill, so every
`post` row predating it holds NULL. `app/models.py:1723`'s `default=0` is a
Python-side default: it applies to rows the ORM inserts and never to rows
already on disk. Five of the six sites that do arithmetic on the column are
unguarded, and `None - 1` raises TypeError, which Flask renders as a 500.

WHAT THIS MIGRATION DOES NOT DO. It repairs existing data. It does not make
those five sites None-safe, and a row explicitly set to NULL after the
migration still raises. So the pair below is not "the crash goes away" -- it
is "the crash is real, and the revision's own SQL repairs the row that causes
it". Since D543's code half the two local decrements are guarded like the
federated one, so the crash below is now the restore increments' alone.
"""
import pytest
from sqlalchemy import text

from app import db
from app.constants import SRC_API
from app.models import PostReply
from app.shared.reply import mod_remove_reply
from tests.factories import (bearer, make_community, make_community_member,
                             make_instance, make_post, make_post_reply,
                             make_user)


BACKFILL_SQL = ('UPDATE post SET reply_count_cross_posted = reply_count '
                'WHERE reply_count_cross_posted IS NULL')


def _seed_removable_reply():
    """A moderator, an author, a local community, a post and a reply on it.

    `mod_remove_reply` (app/shared/reply.py:416) needs the actor to pass
    `reply.community.is_moderator(user)`, and reaches
    `reply.post.reply_count_cross_posted -= 1` at :431 only when the reply's
    author is not a bot -- `make_user` leaves `bot` at its default, which is
    false, so the guard at :429 is open.

    ORDER MATTERS HERE. `make_community` (tests/factories.py:141-142)
    hardcodes `instance_id=1, user_id=1`, so an Instance and a User must
    already occupy id 1 when it is called. The instance and the id-1 burn
    below fill both seats, which is why the burn is load-bearing twice over:
    it keeps `moderator` off the id-1 admin shortcut AND gives the community
    an owner that exists.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    moderator = make_user(instance, 'mod', local=True)
    author = make_user(instance, 'author', local=True)
    community = make_community('microblogs')
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, author, 'https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, 'a reply')
    db.session.commit()
    return moderator, post, reply


def test_a_null_cross_posted_count_no_longer_makes_mod_remove_reply_raise(app, db_session):
    """The row the migration exists for. A NULL left after it (an explicit
    UPDATE; the server default only covers INSERT) used to raise TypeError in
    mod_remove_reply; since D543's code half the decrement is guarded, so the
    removal succeeds and the NULL is left for the backfill."""
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count_cross_posted = NULL '
                            'WHERE id = :id'), {'id': post.id})
    db.session.commit()
    db.session.expire_all()

    mod_remove_reply(reply.id, 'spam', SRC_API, bearer(moderator))

    db.session.expire_all()
    assert db.session.get(PostReply, reply.id).deleted is True


def test_the_backfill_sql_repairs_a_null_row_to_reply_count(app, db_session):
    """The revision's own UPDATE, applied to the row that crashes.

    reply_count, not zero: app/models.py:3108 defines
    `post.reply_count_cross_posted = post.reply_count` for a post with no
    cross-posts, and :3100-3104 sums reply_count across the cross-post set
    when there are. A backfill to 0 satisfies the type and violates that.

    The reply_count is set to 7 rather than left at whatever make_reply
    produces, so a backfill to 0 AND a backfill that merely copies a
    coincidentally-equal value both fail this assertion.
    """
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count = 7, '
                            'reply_count_cross_posted = NULL WHERE id = :id'),
                       {'id': post.id})
    db.session.commit()

    db.session.execute(text(BACKFILL_SQL))
    db.session.commit()
    db.session.expire_all()

    repaired = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert repaired == 7

    mod_remove_reply(reply.id, 'spam', SRC_API, bearer(moderator))
    db.session.expire_all()
    after = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert after == 6


def test_the_backfill_leaves_a_non_null_row_alone(app, db_session):
    """The WHERE clause is load-bearing.

    Without `WHERE reply_count_cross_posted IS NULL` the migration would
    overwrite every correctly-maintained cross-post count with the post's own
    reply_count, silently destroying the sum at app/models.py:3100-3104. This
    is the positive control for that clause: a row whose two counts DISAGREE,
    which a clause-less UPDATE would flatten.
    """
    moderator, post, reply = _seed_removable_reply()
    db.session.execute(text('UPDATE post SET reply_count = 7, '
                            'reply_count_cross_posted = 99 WHERE id = :id'),
                       {'id': post.id})
    db.session.commit()

    db.session.execute(text(BACKFILL_SQL))
    db.session.commit()

    untouched = db.session.execute(
        text('SELECT reply_count_cross_posted FROM post WHERE id = :id'),
        {'id': post.id}).scalar()
    assert untouched == 99


def test_the_column_has_a_server_default_of_zero(app, db_session):
    """An INSERT that omits the column must not produce a new NULL.

    Raw SQL rather than the ORM on purpose: the ORM already supplies
    `default=0` (app/models.py:1723), so an ORM insert would pass this test
    with or without the migration -- mechanism (a) of the five false
    witnesses, asserting on state something else sets unconditionally. This
    INSERT names no counter column at all, so only a database-level default
    can satisfy it.
    """
    instance = make_instance('remote.example')
    author = make_user(instance, 'author', local=True)
    community = make_community('microblogs')
    db.session.commit()

    db.session.execute(text(
        'INSERT INTO post (user_id, community_id, title, type, posted_at, '
        'last_active, ap_id) VALUES (:u, :c, :t, 0, now(), now(), :ap)'),
        {'u': author.id, 'c': community.id, 't': 'a post',
         'ap': 'https://test.piefed.local/post/default-probe'})
    db.session.commit()

    value = db.session.execute(text(
        "SELECT reply_count_cross_posted FROM post WHERE ap_id = :ap"),
        {'ap': 'https://test.piefed.local/post/default-probe'}).scalar()
    assert value == 0
