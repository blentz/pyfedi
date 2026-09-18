"""app/shared/feed.py's `edit_feed` -- Group C, the module's last function.

MEASUREMENT BASIS. Before this file existed, NO test executed edit_feed: the
per-test contexts run in sub-project 50 reported zero contexts for :260-385, on
the run that reported 5219 passed, 3 skipped, 6 subtests passed in 342.05s.
`/usr/bin/grep -rln "edit_feed" tests/` returned four files and none of them
called it -- three mention it in prose and the fourth is about feed_edit, the
ROUTE. That mention-only shape is the trap this round had to name: a grep for
the name finds files, and none of them is an oracle.

g.site. edit_feed:357 and :359 read g.site, and before_request does not run in a
bare test_request_context, so every test here sets it by hand through _site_ctx
below. A test that forgets gets AttributeError on a Flask g with no site.

THE NAME COLLISION, STILL. tests/factories.py:156 defines make_feed and
app/shared/feed.py:172 defines a different one; the factory is imported as
make_feed_factory so the bare name always means production.
"""
import pytest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import g

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import (Community, Feed, FeedItem, FeedJoinRequest, FeedMember, File,
                        Role, Site, User, user_role)
from app.shared.feed import edit_feed
from tests.factories import (make_community, make_feed_item, make_feed_join_request,
                             make_feed_member, make_instance, make_site, make_user,
                             web_ctx)
from tests.factories import make_feed as make_feed_factory


@contextmanager
def _site_ctx(app, user):
    """web_ctx, plus the g.site that edit_feed:357 and :359 read.

    before_request -- which populates g.site in the real app -- does not run
    inside a test_request_context, so the Site row has to be put on g by hand.
    The row itself is minted by _seed via make_site().
    """
    with web_ctx(app, user):
        g.site = Site.query.get(1)
        yield


def _burn_a_seed():
    """Consume User id 1 so nothing under test inherits the id-1 admin trap.

    app/models.py:1259-1261 treats id 1 as an admin, and edit_feed:311 and :367
    both read is_admin(), so an accidental admin would silently satisfy two
    different guards. The assert is live.
    """
    instance = make_instance('burn.piefed.local')
    burn = make_user(instance, 'burnseat')
    assert burn.id == 1
    return instance


def _make_admin(user: User) -> Role:
    """A role named exactly 'Admin' -- what is_admin() looks for once the id-1
    seat is burned (app/models.py:1258-1264)."""
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def _seed():
    """Two feeds with two owners, and the Site row edit_feed reads.

    The SECOND feed is not decoration: the defect this round was scoped around
    deletes rows belonging to feeds other than the one being edited, and a
    fixture with one feed cannot see it. Every id is minted so that no two of
    feed.id, other_feed.id, owner.id, member.id and stranger.id coincide, and
    the separation is asserted live (D653, fact 272).
    """
    instance = _burn_a_seed()
    owner = make_user(instance, 'feedowner')
    member = make_user(instance, 'feedmember')
    stranger = make_user(instance, 'stranger')
    # Four throwaway feeds, counted rather than guessed: the four users above
    # take ids 1-4, so the two real feeds have to land at 5 and 6 for no id to
    # coincide with a user's. Feed and User have separate sequences, which is
    # exactly what makes the coincidence easy to ship (D653).
    for n in range(1, 5):
        make_feed_factory(instance, name=f'idparitybreaker{n}')
    feed = make_feed_factory(instance, name='editablefeed', public=True)
    feed.user_id = owner.id
    other_feed = make_feed_factory(instance, name='someoneelsesfeed', public=True)
    other_feed.user_id = stranger.id
    make_site()
    db.session.commit()

    assert len({feed.id, other_feed.id, owner.id, member.id, stranger.id}) == 5
    return SimpleNamespace(instance=instance, owner=owner, member=member,
                           stranger=stranger, feed=feed, other_feed=other_feed)


def _form(**overrides):
    """The form shape edit_feed's SRC_WEB arm reads at app/shared/feed.py:276-288.

    A stub rather than the real EditFeedForm: the production arm only reads
    `.data` off each field, and the icon/banner uploads arrive as separate
    arguments rather than form fields.
    """
    fields = {'url': 'editablefeed', 'title': 'Edited', 'public': True, 'description': '',
              'nsfw': False, 'nsfl': False, 'communities': '', 'is_instance_feed': False,
              'show_child_posts': False, 'parent_feed_id': None}
    fields.update(overrides)
    return SimpleNamespace(**{k: SimpleNamespace(data=v) for k, v in fields.items()})


# --------------------------------------------------------------------------
# Task 1: P1, P2 and P3 -- the public-to-private block.
# --------------------------------------------------------------------------


def test_going_private_unsubscribes_only_this_feeds_members(app, db_session):
    """Was a PIN; INVERTED once the delete was scoped.

    ORIGINAL PINNED CLAIM, now false: ":363's delete filters on is_owner alone,
    with NO feed_id, so making one feed private unsubscribes every non-owner
    member of every feed on the instance."

    The second feed is the control and it is the whole point: a one-feed
    fixture cannot distinguish a scoped delete from a global one, which is why
    nothing caught this. Its member row must survive, its owner row must
    survive, and this feed's own owner must survive while its member goes.
    """
    s = _seed()
    make_feed_member(s.owner, s.feed, is_owner=True)
    make_feed_member(s.member, s.feed)
    make_feed_member(s.stranger, s.other_feed, is_owner=True)
    make_feed_member(s.member, s.other_feed)
    db.session.commit()
    feed_id, other_feed_id = s.feed.id, s.other_feed.id

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(public=False), s.feed, SRC_WEB)

    assert FeedMember.query.filter_by(feed_id=other_feed_id, is_owner=False).count() == 1
    assert FeedMember.query.filter_by(feed_id=other_feed_id, is_owner=True).count() == 1
    assert FeedMember.query.filter_by(feed_id=feed_id, is_owner=False).count() == 0
    assert FeedMember.query.filter_by(feed_id=feed_id, is_owner=True).count() == 1


def test_going_private_clears_this_feeds_pending_requests(app, db_session):
    """Was a PIN; INVERTED once the join-request delete was re-filtered.

    ORIGINAL PINNED CLAIM, now false: ":364 filters FeedJoinRequest by the
    EDITOR's user id, so it removes the row belonging to the person doing the
    editing and leaves the feed's pending requests in place."

    Three rows, one per thing that has to be true: the member's request on this
    feed is cleared, the editor's own row on this feed is cleared too (it is
    this feed's row, whoever it belongs to), and a THIRD user's request on
    ANOTHER feed survives, which is what keeps the new feed_id filter
    load-bearing.

    Clearing a pending request leaves that user at SUBSCRIPTION_NONMEMBER
    rather than SUBSCRIPTION_PENDING, which D674 established is the correct
    state for someone with no membership -- so this repair and D674's agree.
    """
    s = _seed()
    make_feed_member(s.owner, s.feed, is_owner=True)
    make_feed_join_request(s.member, s.feed)
    make_feed_join_request(s.owner, s.feed)
    make_feed_join_request(s.member, s.other_feed)
    db.session.commit()
    feed_id, other_feed_id = s.feed.id, s.other_feed.id

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(public=False), s.feed, SRC_WEB)

    assert FeedJoinRequest.query.filter_by(feed_id=feed_id).count() == 0
    assert FeedJoinRequest.query.filter_by(user_id=s.member.id,
                                           feed_id=other_feed_id).count() == 1


def test_going_private_counts_only_this_feeds_members(app, db_session):
    """Was a PIN; INVERTED once the count was scoped.

    ORIGINAL PINNED CLAIM, now false: ":365 assigns subscriptions_count from a
    GLOBAL count of non-owner memberships", which read 0 only because the
    delete above it had just emptied the table.

    1, not 0, is the number that distinguishes the two queries: after the
    scoped delete this feed has exactly its owner, and subscriptions_count
    counts the owner elsewhere in this module -- make_feed:228 writes 1 for a
    feed that has only its owner. The other feed keeps two rows, so a count
    that had stayed global would read 2 and a count that excluded owners would
    read 0.
    """
    s = _seed()
    make_feed_member(s.owner, s.feed, is_owner=True)
    make_feed_member(s.member, s.feed)
    make_feed_member(s.stranger, s.other_feed, is_owner=True)
    make_feed_member(s.member, s.other_feed)
    db.session.commit()

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(public=False), s.feed, SRC_WEB)

    assert Feed.query.get(s.feed.id).subscriptions_count == 1
    assert FeedMember.query.filter_by(feed_id=s.other_feed.id).count() == 2


# --------------------------------------------------------------------------
# Task 2: P4 -- the ownership check that runs after the writes.
# --------------------------------------------------------------------------


def test_edit_feed_rewrites_the_feed_before_it_checks_who_is_asking(app, db_session):
    """PIN (P4): :292-305 assign name, machine_name, title, description,
    description_html, show_posts_in_children and parent_feed_id; :311 then
    decides whether the caller may edit the feed at all and raises
    Exception('incorrect_login').

    The raise does not roll back, so the rejected values sit on the live ORM
    object and the NEXT commit in the same session writes them. That last
    assertion is the defect: asserting only the raise passes against a correct
    implementation too.

    The API path reaches this with an arbitrary caller's data --
    app/api/alpha/utils/feed.py:202's put_feed has no ownership check of its
    own, so :311 is the only gate there is.
    """
    s = _seed()
    stored_title = Feed.query.get(s.feed.id).title

    with _site_ctx(app, s.stranger):
        with pytest.raises(Exception, match='incorrect_login'):
            edit_feed(_form(title='Hijacked'), s.feed, SRC_WEB)
        assert s.feed.title == 'Hijacked'
        db.session.commit()

    assert Feed.query.get(s.feed.id).title == 'Hijacked' != stored_title
