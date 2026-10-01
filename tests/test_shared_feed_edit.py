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
        g.site = db.session.get(Site, 1)
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

    assert db.session.get(Feed, s.feed.id).subscriptions_count == 1
    assert FeedMember.query.filter_by(feed_id=s.other_feed.id).count() == 2


# --------------------------------------------------------------------------
# Task 2: P4 -- the ownership check that runs after the writes.
# --------------------------------------------------------------------------


def test_edit_feed_refuses_a_stranger_without_writing_anything(app, db_session):
    """Was a PIN; INVERTED once the ownership check moved above the writes.

    ORIGINAL PINNED CLAIM, now false: ":292-305 assign name, machine_name,
    title, description, description_html, show_posts_in_children and
    parent_feed_id; :311 then decides whether the caller may edit the feed at
    all", so the rejected values sat on the live ORM object and the next commit
    in the same session wrote them.

    The commit after the refusal is the assertion that matters: a check that
    raised late would still raise, and only the stored value tells the two
    apart. The API path reaches this with an arbitrary caller's data --
    app/api/alpha/utils/feed.py:202's put_feed has no ownership check of its
    own, so this is the only gate there is.
    """
    s = _seed()
    stored_title = db.session.get(Feed, s.feed.id).title

    with _site_ctx(app, s.stranger):
        with pytest.raises(Exception, match='incorrect_login'):
            edit_feed(_form(title='Hijacked'), s.feed, SRC_WEB)
        assert s.feed.title == stored_title
        db.session.commit()

    assert db.session.get(Feed, s.feed.id).title == stored_title


def test_edit_feed_lets_the_owner_through(app, db_session):
    """The positive control for the check above. Without it, a "fix" that
    refused everybody would pass every assertion in the refusal test."""
    s = _seed()
    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(title='Owner edit'), s.feed, SRC_WEB)

    assert db.session.get(Feed, s.feed.id).title == 'Owner edit'


def test_edit_feed_lets_an_admin_through(app, db_session):
    """:311's second disjunct, `user.is_admin()`. Since D697 (owner ruling)
    the web route admits an admin too, so both paths agree.
    """
    s = _seed()
    _make_admin(s.stranger)
    assert s.stranger.is_admin() and s.feed.user_id != s.stranger.id

    with _site_ctx(app, s.stranger):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(title='Admin edit'), s.feed, SRC_WEB)

    assert db.session.get(Feed, s.feed.id).title == 'Admin edit'


def test_edit_feed_from_scratch_still_checks_ownership(app, db_session):
    """D694, fixed: `from_scratch=True` skipped the ownership check, so a
    caller passing it could edit any feed (latent: no caller passes it). The
    check now runs whatever from_scratch is; the feed is untouched."""
    s = _seed()
    original_title = s.feed.title
    with _site_ctx(app, s.stranger):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            with pytest.raises(Exception, match='incorrect_login'):
                edit_feed(_form(title='No check at all'), s.feed, SRC_WEB, from_scratch=True)

    db.session.rollback()
    assert db.session.get(Feed, s.feed.id).title == original_title


# --------------------------------------------------------------------------
# Task 3: the field-copy half -- :261-316.
# --------------------------------------------------------------------------


def _api_payload(**overrides):
    """The dict shape edit_feed's SRC_API arm reads at app/shared/feed.py:262-273.

    Every key is read unconditionally -- no .get() anywhere in that arm -- so a
    payload missing one raises KeyError. app/api/alpha/utils/feed.py:186-200 is
    where the real one is built.
    """
    payload = {'url': 'editablefeed', 'title': 'Edited', 'public': True, 'description': '',
               'icon_url': None, 'banner_url': None, 'nsfw': False, 'nsfl': False,
               'communities': '', 'is_instance_feed': False, 'show_child_posts': False,
               'parent_feed_id': None}
    payload.update(overrides)
    return payload


def test_edit_feed_api_arm_writes_every_derived_field(app, db_session):
    """The SRC_API arm end to end, asserting the fields that are not straight
    copies.

    The description is carried through with a CRLF so the markdown conversion
    is observable: the API arm hands edit_feed the RAW description and :310 is
    the only conversion on that path, unlike the web arm where :279 has already
    converted it.

    show_child_posts False, nsfw True and nsfl False: three flags with three
    different values, so a hardcoded constant or a swapped pair is visible.
    """
    from app.utils import piefed_markdown_to_lemmy_markdown
    s = _seed()
    raw = 'first line\r\nsecond line'

    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        # Site.enable_nsfw and enable_nsfl default False on the row make_site()
        # mints, and :365/:367 guard the writes on them, so a test that wants to
        # observe the flags has to turn the site's own switches on first. Both
        # arms of those guards are covered separately below.
        g.site.enable_nsfw = g.site.enable_nsfl = True
        with patch('app.shared.feed.authorise_api_user', return_value=s.owner), \
                patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_api_payload(title='API edit', description=raw, nsfw=True,
                                   show_child_posts=False), s.feed, SRC_API, auth='Bearer x')

    edited = db.session.get(Feed, s.feed.id)
    assert edited.title == 'API edit'
    assert edited.description == piefed_markdown_to_lemmy_markdown(raw) != raw
    # description_html is built from the RAW description (:311 passes
    # `description`, not `feed.description`), so the two differ for any input
    # the markdown conversion touches. Asserting "not None" left that free.
    from app.utils import markdown_to_html
    assert edited.description_html == markdown_to_html(raw)
    assert markdown_to_html(raw) != markdown_to_html(edited.description)
    assert edited.show_posts_in_children is False
    assert edited.nsfw is True and edited.nsfl is False


@pytest.mark.parametrize('url_value, public, expected_name, rewritten', [
    ('RenamedFeed', True, 'renamedfeed', True),
    ('Renamed/ignored', True, 'renamed', True),
    ('RenamedFeed', False, 'renamedfeed/feedowner', True),
    ('', True, 'editablefeed', False),
    (None, True, 'editablefeed', False),
])
def test_edit_feed_slugifies_the_url_and_only_when_one_is_given(app, db_session, url_value,
                                                                public, expected_name,
                                                                rewritten):
    """:303-308. Five rows, each isolating one decision:

    - a mixed-case url is lowercased and slugified;
    - anything after the first '/' is dropped, which is what keeps a private
      feed's 'name/owner' form from growing a second suffix on re-edit;
    - a PRIVATE feed gets '/' + the owner's user_name appended, and the owner's
      name is not a substring of the url, so the composite is distinguishable
      from either half;
    - an empty url and None both leave the name alone, which is the case the
      web route relies on when it disables the field
      (app/feed/routes.py:142-143).

    The last two rows also keep `if url:` observable as False; without them a
    mutant deleting the guard passes.
    """
    s = _seed()
    assert 'feedowner' not in 'renamedfeed'

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(url=url_value, public=public), s.feed, SRC_WEB)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.name == expected_name
    # machine_name is written from the same url at :308 and is left alone
    # otherwise. The factory never sets it, so "left alone" is None -- asserted
    # rather than skipped, because a mutant that wrote machine_name
    # unconditionally would otherwise pass the two no-url rows.
    assert edited.machine_name == (expected_name if rewritten else None)


@pytest.mark.parametrize('parent_given', [True, False, 'zero'])
def test_edit_feed_sets_parent_feed_id_only_when_one_is_given(app, db_session, parent_given):
    """:313-316. The else arm assigns None explicitly, and the 'zero' row is
    what makes that observable: 0 is falsy, so it takes the else arm, and an
    implementation that wrote the given value there would store 0."""
    s = _seed()
    parent = s.other_feed if parent_given is True else None
    given = parent.id if parent else (0 if parent_given == 'zero' else None)

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(parent_feed_id=given), s.feed, SRC_WEB)

    edited = db.session.get(Feed, s.feed.id)
    if parent_given is True:
        assert edited.parent_feed_id == parent.id != edited.id
    else:
        assert edited.parent_feed_id is None


def test_edit_feed_renaming_leaves_the_activitypub_identity_behind(app, db_session):
    """PINNED, REGISTERED AND NOT FIXED (R2): a rename rewrites name and
    machine_name and touches none of the five ap_* urls, so the feed's actor id
    keeps pointing at the old name.

    Not repaired here because rewriting an actor's id mid-life either orphans
    remote followers or needs a Move, and this module has no precedent for
    either -- a federation decision rather than a coverage one.

    THIS ALSO CORRECTS D685, which said Group C "rebuilds these fields in
    edit_feed(from_scratch=True)". It does not rebuild them at all, on any
    value of from_scratch, and no caller passes True.
    """
    s = _seed()
    before = (s.feed.ap_profile_id, s.feed.ap_public_url, s.feed.ap_followers_url)

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(url='renamedfeed'), s.feed, SRC_WEB)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.name == 'renamedfeed'
    assert edited.ap_profile_id == before[0]
    assert edited.ap_public_url == before[1]
    assert edited.ap_followers_url == before[2]
    assert 'editablefeed' in edited.ap_profile_id


# --------------------------------------------------------------------------
# Task 4: the image half -- :318-363.
# --------------------------------------------------------------------------


def _attach_icon(feed, source_url='https://example.test/old-icon.png'):
    """Give `feed` a real File as its icon, and return it.

    :327-328 read feed.icon.source_url and feed.icon.medium_url(), so the
    icon-set arms need a row rather than a bare id: an icon_id pointing at
    nothing raises AttributeError on a None relationship.
    """
    file = File(source_url=source_url, file_path='app/static/media/feeds/old-icon.png')
    db.session.add(file)
    db.session.commit()
    feed.icon_id = file.id
    db.session.commit()
    return file


def _attach_banner(feed, source_url='https://example.test/old-banner.png'):
    """The banner twin of _attach_icon. Written out rather than shared with a
    flag, because :327-331 and :332-337 have already diverged -- the banner arm
    busts Feed.header_image at :336 and the icon arm busts nothing -- and a
    shared helper is what hides the next divergence."""
    file = File(source_url=source_url, file_path='app/static/media/feeds/old-banner.png')
    db.session.add(file)
    db.session.commit()
    feed.image_id = file.id
    db.session.commit()
    return file


@contextmanager
def _api_ctx(app, user):
    """A request context with g.site and authorise_api_user bound to `user`.

    The image tests go through the SRC_API arm on purpose: the web arm derives
    icon_url from `process_upload(uploaded_icon_file) if uploaded_icon_file
    else None` (:280-281), so without an upload it is always None and the
    icon/banner blocks are unreachable from that arm. The API arm takes the url
    straight from the payload, which is the shape under test.
    """
    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        g.site.enable_nsfw = g.site.enable_nsfl = True
        with patch('app.shared.feed.authorise_api_user', return_value=user):
            yield


def test_edit_feed_api_refuses_to_rename_a_feed_with_subscribers(app, db_session):
    """D696, fixed (owner ruling): renaming a feed that has subscribers is
    refused by the server on the API arm too, before anything is written."""
    s = _seed()
    s.feed.subscriptions_count = 2
    db.session.commit()

    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            with pytest.raises(Exception, match='feed_has_subscribers'):
                edit_feed(_api_payload(url='renamedfeed', title='Not saved'), s.feed, SRC_API, auth='Bearer x')

    db.session.expire_all()
    edited = db.session.get(Feed, s.feed.id)
    assert edited.name == 'editablefeed'
    assert edited.title != 'Not saved'


def test_edit_feed_keeping_the_name_of_a_feed_with_subscribers_is_allowed(app, db_session):
    """D696's control: the same url is not a rename, so the edit lands."""
    s = _seed()
    s.feed.subscriptions_count = 2
    db.session.commit()

    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_api_payload(title='Saved'), s.feed, SRC_API, auth='Bearer x')

    assert db.session.get(Feed, s.feed.id).title == 'Saved'


@pytest.mark.parametrize('attach, incoming, expect_replaced', [
    (None, 'https://example.test/new-icon.png', True),
    ('source', 'https://example.test/old-icon.png', False),
    ('medium', 'MEDIUM', False),
    ('source', 'https://example.test/new-icon.png', True),
])
def test_edit_feed_replaces_the_icon_only_when_the_url_really_changed(
        app, db_session, attach, incoming, expect_replaced):
    """:327-331 and :339-344, the icon change detector and the block it gates.

    Four rows, one per state the detector can be in:

    - no icon at all -> :330's `if not feed.icon_id:` sets changed;
    - an icon whose source_url matches the incoming url -> not changed;
    - an icon whose medium_url() matches -> not changed, which is the inner
      :328 test and the only thing that separates it from :327;
    - an icon that matches neither -> changed.

    The third row is the one a three-row version drops, and it is the only row
    where :327 is True and :328 is False.

    make_image_sizes is patched and asserted with its full argument tuple: the
    40/250 pair is the only thing distinguishing this block from the banner
    block sixteen lines below.
    """
    s = _seed()
    existing = _attach_icon(s.feed) if attach else None
    if incoming == 'MEDIUM':
        incoming = existing.medium_url()
    before_icon_id = s.feed.icon_id

    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=True), \
                patch('app.shared.feed.make_image_sizes') as sizer, \
                patch('app.models.File.delete_from_disk') as unlink:
            edit_feed(_api_payload(icon_url=incoming), s.feed, SRC_API, auth='Bearer x')

    edited = db.session.get(Feed, s.feed.id)
    if expect_replaced:
        # Was a PIN; INVERTED once the assignment moved to the relationship.
        #
        # ORIGINAL PINNED CLAIM, now false: "replacing an icon the feed ALREADY
        # had loses it -- the feed ends with icon_id None and the new File row
        # orphaned", because the FK attribute was assigned while the loaded
        # `icon` relationship still pointed at the old File and won at flush.
        #
        # The old row is gone either way; what changed is that the feed now
        # keeps the icon it was given. The old row's disk file is unlinked
        # exactly when there was an old row.
        assert edited.icon_id == File.query.filter_by(source_url=incoming).one().id
        assert sizer.call_args.args == (edited.icon_id, 40, 250, 'feeds', False)
        assert unlink.call_count == (1 if attach else 0)
        if attach:
            assert db.session.get(File, before_icon_id) is None
    else:
        assert edited.icon_id == before_icon_id
        assert sizer.call_count == 0
        assert unlink.call_count == 0


@pytest.mark.parametrize('attach, incoming, expect_replaced', [
    (None, 'https://example.test/new-banner.png', True),
    ('source', 'https://example.test/old-banner.png', False),
    ('medium', 'MEDIUM', False),
    ('source', 'https://example.test/new-banner.png', True),
])
def test_edit_feed_replaces_the_banner_only_when_the_url_really_changed(
        app, db_session, attach, incoming, expect_replaced):
    """:332-337 and :351-363, the banner twin -- written out separately rather
    than shared with the icon test, because the two have already diverged:
    :336 busts Feed.header_image when there is no banner and :363 busts it
    again after deleting the old one, while the icon arm busts nothing at
    either point. A shared helper would hide the next divergence.

    The size pair asserted here is 878/1600, the banner's own.
    """
    s = _seed()
    existing = _attach_banner(s.feed) if attach else None
    if incoming == 'MEDIUM':
        incoming = existing.medium_url()
    before_image_id = s.feed.image_id

    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=True), \
                patch('app.shared.feed.make_image_sizes') as sizer, \
                patch('app.models.File.delete_from_disk') as unlink:
            edit_feed(_api_payload(banner_url=incoming), s.feed, SRC_API, auth='Bearer x')

    edited = db.session.get(Feed, s.feed.id)
    if expect_replaced:
        assert edited.image_id != before_image_id
        assert db.session.get(File, edited.image_id).source_url == incoming
        assert sizer.call_args.args == (edited.image_id, 878, 1600, 'feeds', False)
        assert unlink.call_count == (1 if attach else 0)
        if attach:
            assert db.session.get(File, before_image_id) is None
    else:
        assert edited.image_id == before_image_id
        assert sizer.call_count == 0
        assert unlink.call_count == 0


@pytest.mark.parametrize('is_image', [True, False])
def test_edit_feed_stores_an_icon_only_when_the_url_is_one(app, db_session, is_image):
    """:339's third operand, `is_image_url(icon_url)`, isolated from the first
    two: the url is present and the change detector says changed, so only the
    image test can decide the outcome."""
    s = _seed()
    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=is_image), \
                patch('app.shared.feed.make_image_sizes'), \
                patch('app.models.File.delete_from_disk'):
            edit_feed(_api_payload(icon_url='https://example.test/thing'), s.feed,
                      SRC_API, auth='Bearer x')

    assert (db.session.get(Feed, s.feed.id).icon_id is not None) is is_image


def test_edit_feed_from_scratch_stores_the_icon_without_consulting_the_detector(app, db_session):
    """:339's `(from_scratch or icon_url_changed)` disjunct, reached through
    from_scratch rather than through the detector.

    With from_scratch=True the whole :321-337 block is skipped, so
    icon_url_changed stays False from :290 and the disjunct is the only thing
    that can let the write through. This is also the arm that proves the
    disjunct is not dead weight -- and R1 records that no production caller
    reaches it.

    THE OLD ROW IS REMOVED HERE TOO, AND THAT IS A DELIBERATE CHANGE THIS ROUND
    MADE RATHER THAN AN ACCIDENT. Before the icon repair, from_scratch kept the
    previous File row and its disk file. Attaching the new icon through the
    relationship makes the delete-orphan cascade drop the old row on every
    path, so the repair unlinks the disk file on every path too -- leaving it
    would orphan a file nothing references. Nothing in production changes,
    because no caller passes from_scratch=True (R1).
    """
    s = _seed()
    existing = _attach_icon(s.feed)
    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=True), \
                patch('app.shared.feed.make_image_sizes'), \
                patch('app.models.File.delete_from_disk') as unlink:
            edit_feed(_api_payload(icon_url='https://example.test/scratch.png'), s.feed,
                      SRC_API, auth='Bearer x', from_scratch=True)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.icon_id != existing.id
    assert db.session.get(File, existing.id) is None
    assert unlink.call_count == 1


def test_edit_feed_cannot_be_given_an_old_file_id_whose_row_has_vanished(app, db_session):
    """THE ROUND'S ONE RESIDUAL ARC, WITH ITS PROOF: the banner block's
    `if remove_file:` False arm at :371 is UNREACHABLE.

    The arm guards against an image_id naming a File row that is gone, and the
    two columns make that state unreachable by two DIFFERENT mechanisms, which
    is why they are demonstrated separately rather than in one delete:

    - Feed.icon carries `backref='feed'` as well as single_parent and
      delete-orphan (app/models.py:4137), so deleting the File row makes the
      ORM null feed.icon_id in the same flush.
    - Feed.image has no backref (`:4138`), so the ORM does not null it, and the
      database refuses the delete outright: ForeignKeyViolation on
      feed_image_id_fkey.

    Either way the guarded state cannot be produced. Registered as fact 75
    CAUSE 5, unreachable data; the arc [371, 376] stays missing and no test
    pretends to cover it.

    The icon block no longer has the equivalent arc at all: this round's repair
    attaches the new File through the relationship and lets the cascade remove
    the old row, so no `File.query.get(old_id)` remains on that path.
    """
    from sqlalchemy.exc import IntegrityError
    s = _seed()

    icon = _attach_icon(s.feed)
    icon_id = icon.id
    db.session.delete(icon)
    db.session.commit()
    assert db.session.get(File, icon_id) is None
    assert db.session.get(Feed, s.feed.id).icon_id is None

    banner = _attach_banner(s.feed)
    banner_id = banner.id
    db.session.delete(banner)
    with pytest.raises(IntegrityError, match='feed_image_id_fkey'):
        db.session.commit()
    db.session.rollback()
    assert db.session.get(File, banner_id) is not None


# --------------------------------------------------------------------------
# Task 5: the tail -- :376-414.
# --------------------------------------------------------------------------


@pytest.mark.parametrize('site_nsfw, site_nsfl', [(True, True), (False, False)])
def test_edit_feed_clears_the_nsfw_flags_whatever_the_site_allows(
        app, db_session, site_nsfw, site_nsfl):
    """D698, fixed: the site switches guarded both directions, so a feed
    flagged nsfw/nsfl before an admin turned the switch off could never be
    cleared by its owner. Clearing is now always allowed; only setting the
    flag needs the switch. Starting at True and submitting False clears it in
    every site state.
    """
    s = _seed()
    s.feed.nsfw = True
    s.feed.nsfl = True
    db.session.commit()

    with _site_ctx(app, s.owner):
        g.site.enable_nsfw = site_nsfw
        g.site.enable_nsfl = site_nsfl
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(nsfw=False, nsfl=False), s.feed, SRC_WEB)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.nsfw is False
    assert edited.nsfl is False


def test_edit_feed_cannot_set_the_nsfw_flags_while_the_site_disables_them(app, db_session):
    """The other half of D698: the SET direction stays behind the switches."""
    s = _seed()
    s.feed.nsfw = False
    s.feed.nsfl = False
    db.session.commit()

    with _site_ctx(app, s.owner):
        g.site.enable_nsfw = False
        g.site.enable_nsfl = False
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(nsfw=True, nsfl=True), s.feed, SRC_WEB)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.nsfw is False
    assert edited.nsfl is False


@pytest.mark.parametrize('was_public, now_public, expect_block', [
    (True, False, True),
    (True, True, False),
    (False, True, False),
    (False, False, False),
])
def test_edit_feed_clears_the_members_only_on_the_public_to_private_transition(
        app, db_session, was_public, now_public, expect_block):
    """:370's `feed.public and not public`, swept over both operands.

    Four rows, and each kills a different mutant: dropping the first operand is
    caught by (False, False), dropping the second by (True, True), and `and` ->
    `or` by either row where exactly one side is true. Three rows would leave
    one of them free -- the lockstep gap sub-project 50 shipped at leave_feed's
    flash guard.

    The member row is the observable: it survives every row but the transition.
    """
    s = _seed()
    s.feed.public = was_public
    make_feed_member(s.owner, s.feed, is_owner=True)
    make_feed_member(s.member, s.feed)
    db.session.commit()

    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(public=now_public), s.feed, SRC_WEB)

    remaining = FeedMember.query.filter_by(feed_id=s.feed.id, is_owner=False).count()
    assert remaining == (0 if expect_block else 1)
    assert db.session.get(Feed, s.feed.id).public is now_public


@pytest.mark.parametrize('is_admin', [True, False])
def test_edit_feed_lets_only_an_admin_change_the_instance_feed_flag(app, db_session, is_admin):
    """:386-388. The admin gate edit_feed has always had, and make_feed did not
    until sub-project 50 added it (D675) -- the drift that duplication rather
    than delegation produced.

    The feed starts with is_instance_feed False and the edit submits True, so
    the stored value names which arm ran. The menu cache bust beside it is
    asserted as a CALL: CACHE_TYPE is NullCache in tests, so its effect is
    unobservable by construction (D602).
    """
    s = _seed()
    editor = s.owner
    if is_admin:
        _make_admin(editor)
    assert editor.is_admin() is is_admin
    # The feed starts True in the second half of this test so the admin arm has
    # to be able to CLEAR the flag as well as set it; a hardcoded True passes
    # every set-only test.

    with _site_ctx(app, editor):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.cache.delete_memoized') as bust:
            edit_feed(_form(is_instance_feed=True), s.feed, SRC_WEB)

    assert db.session.get(Feed, s.feed.id).is_instance_feed is is_admin
    # The menu bust is identified by its ARGUMENT, not by a call count: :335's
    # `if not feed.image_id:` fires delete_memoized(Feed.header_image, feed) on
    # every row here, so a count assertion would be measuring that instead.
    from app.utils import menu_instance_feeds
    busted = [call.args[0] for call in bust.call_args_list]
    assert (menu_instance_feeds in busted) is is_admin

    # Second half: the same editor submitting False. Only the admin arm can
    # clear it, and a hardcoded True would keep it set.
    feed = db.session.get(Feed, s.feed.id)
    feed.is_instance_feed = True
    db.session.commit()
    with _site_ctx(app, editor):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(is_instance_feed=False), feed, SRC_WEB)
    assert db.session.get(Feed, s.feed.id).is_instance_feed is not is_admin


def test_edit_feed_adds_and_removes_communities_by_the_set_difference(app, db_session):
    """:401-414. existing_communities and form_communities_to_ids are Group A's
    and already covered, so both are patched and this test asserts DISPATCH.

    One id in both sets is the control: it must be neither added nor removed,
    which is what makes this a set difference rather than "add everything
    submitted and remove everything stored". The ids are decoys chosen to
    collide with no row in the fixture, and feed.id and user.id are asserted
    distinct from them, because this call passes four ids adjacently (D653).
    """
    s = _seed()
    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.existing_communities', return_value=[61, 62]) as existing, \
                patch('app.shared.feed.form_communities_to_ids',
                      return_value={62, 63}) as resolver, \
                patch('app.shared.feed._feed_add_community') as adder, \
                patch('app.shared.feed._feed_remove_community') as remover:
            edit_feed(_form(communities='!a@b\n!c@d'), s.feed, SRC_WEB)

    assert existing.call_args.args == (s.feed.id,)
    assert resolver.call_args.args == ('!a@b\n!c@d',)
    assert adder.call_count == 1
    assert adder.call_args.args == (63, 0, s.feed.id, s.owner.id)
    assert remover.call_count == 1
    assert remover.call_args.args == (61, s.feed.id)
    assert len({61, 62, 63, s.feed.id, s.owner.id}) == 5


@pytest.mark.parametrize('is_image', [True, False])
def test_edit_feed_stores_a_banner_only_when_the_url_is_one(app, db_session, is_image):
    """:362's third operand, `is_image_url(banner_url)`, isolated the way the
    icon's is. Written out rather than folded into the icon test: these two
    blocks have already diverged twice -- the banner busts Feed.header_image
    and deletes its old row by id, the icon does neither -- and a shared
    parametrisation would hide the next divergence."""
    s = _seed()
    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=is_image), \
                patch('app.shared.feed.make_image_sizes'), \
                patch('app.models.File.delete_from_disk'):
            edit_feed(_api_payload(banner_url='https://example.test/thing'), s.feed,
                      SRC_API, auth='Bearer x')

    assert (db.session.get(Feed, s.feed.id).image_id is not None) is is_image


def test_edit_feed_from_scratch_removes_the_old_banner_row(app, db_session):
    """D699, fixed: the banner block deleted its old row only when not
    from_scratch, so a from_scratch replacement left the previous File
    orphaned, where the icon block's cascade removes it on every path. The
    banner's old row and disk file are now removed whatever from_scratch is.
    """
    s = _seed()
    existing = _attach_banner(s.feed)
    old_id = existing.id

    with _api_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.is_image_url', return_value=True), \
                patch('app.shared.feed.make_image_sizes'), \
                patch('app.models.File.delete_from_disk') as unlink:
            edit_feed(_api_payload(banner_url='https://example.test/scratch-banner.png'),
                      s.feed, SRC_API, auth='Bearer x', from_scratch=True)

    edited = db.session.get(Feed, s.feed.id)
    assert edited.image_id != old_id
    assert db.session.get(File, old_id) is None
    assert unlink.call_count == 1
