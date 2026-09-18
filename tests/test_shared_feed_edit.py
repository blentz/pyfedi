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
    stored_title = Feed.query.get(s.feed.id).title

    with _site_ctx(app, s.stranger):
        with pytest.raises(Exception, match='incorrect_login'):
            edit_feed(_form(title='Hijacked'), s.feed, SRC_WEB)
        assert s.feed.title == stored_title
        db.session.commit()

    assert Feed.query.get(s.feed.id).title == stored_title


def test_edit_feed_lets_the_owner_through(app, db_session):
    """The positive control for the check above. Without it, a "fix" that
    refused everybody would pass every assertion in the refusal test."""
    s = _seed()
    with _site_ctx(app, s.owner):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(title='Owner edit'), s.feed, SRC_WEB)

    assert Feed.query.get(s.feed.id).title == 'Owner edit'


def test_edit_feed_lets_an_admin_through(app, db_session):
    """:311's second disjunct, `user.is_admin()`.

    REGISTERED DIVERGENCE (R4), asserted here as current behaviour rather than
    corrected: the web route refuses anyone who is not the owner
    (app/feed/routes.py:133 aborts 404, admin or not), so an admin can edit
    another user's feed through the API and not through the UI. Neither
    behaviour is obviously the intended one, so this round pins what is there.
    """
    s = _seed()
    _make_admin(s.stranger)
    assert s.stranger.is_admin() and s.feed.user_id != s.stranger.id

    with _site_ctx(app, s.stranger):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(title='Admin edit'), s.feed, SRC_WEB)

    assert Feed.query.get(s.feed.id).title == 'Admin edit'


def test_edit_feed_from_scratch_skips_the_ownership_check_entirely(app, db_session):
    """PINNED, REGISTERED AND NOT FIXED (R1): `from_scratch=True` skips the
    check, so a caller that passes it edits any feed.

    Latent: no caller passes True. `/usr/bin/grep -rn "from_scratch" app/`
    finds the True-passing call sites only in app/shared/post.py:243 and
    app/shared/community.py:282, whose make_* functions delegate to their
    edit_* twin; make_feed does not delegate, it duplicates, which is the same
    divergence that left make_feed accepting is_instance_feed from anyone
    (D675) while edit_feed has always gated it.

    Pinned rather than repaired so that a future caller which starts passing
    True fails a test rather than a review.
    """
    s = _seed()
    with _site_ctx(app, s.stranger):
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_form(title='No check at all'), s.feed, SRC_WEB, from_scratch=True)

    assert Feed.query.get(s.feed.id).title == 'No check at all'


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
        g.site = Site.query.get(1)
        # Site.enable_nsfw and enable_nsfl default False on the row make_site()
        # mints, and :365/:367 guard the writes on them, so a test that wants to
        # observe the flags has to turn the site's own switches on first. Both
        # arms of those guards are covered separately below.
        g.site.enable_nsfw = g.site.enable_nsfl = True
        with patch('app.shared.feed.authorise_api_user', return_value=s.owner), \
                patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            edit_feed(_api_payload(title='API edit', description=raw, nsfw=True,
                                   show_child_posts=False), s.feed, SRC_API, auth='Bearer x')

    edited = Feed.query.get(s.feed.id)
    assert edited.title == 'API edit'
    assert edited.description == piefed_markdown_to_lemmy_markdown(raw) != raw
    assert edited.description_html is not None
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

    edited = Feed.query.get(s.feed.id)
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

    edited = Feed.query.get(s.feed.id)
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

    edited = Feed.query.get(s.feed.id)
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
        g.site = Site.query.get(1)
        g.site.enable_nsfw = g.site.enable_nsfl = True
        with patch('app.shared.feed.authorise_api_user', return_value=user):
            yield


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

    edited = Feed.query.get(s.feed.id)
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
            assert File.query.get(before_icon_id) is None
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

    edited = Feed.query.get(s.feed.id)
    if expect_replaced:
        assert edited.image_id != before_image_id
        assert File.query.get(edited.image_id).source_url == incoming
        assert sizer.call_args.args == (edited.image_id, 878, 1600, 'feeds', False)
        assert unlink.call_count == (1 if attach else 0)
        if attach:
            assert File.query.get(before_image_id) is None
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

    assert (Feed.query.get(s.feed.id).icon_id is not None) is is_image


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

    edited = Feed.query.get(s.feed.id)
    assert edited.icon_id != existing.id
    assert File.query.get(existing.id) is None
    assert unlink.call_count == 1


def test_edit_feed_cannot_be_given_an_old_file_id_whose_row_has_vanished(app, db_session):
    """:348's `if remove_file:` False arm is UNREACHABLE, and this test is the
    proof rather than an assertion about edit_feed.

    Feed.icon is declared with `single_parent=True, cascade="all,
    delete-orphan"` (app/models.py:4137), so deleting the File row also clears
    feed.icon_id in the same flush -- the state :348 guards against, an
    icon_id naming a row that is gone, cannot be reached through the ORM at
    all. Demonstrated here by deleting the row and reading the feed back.

    Registered as fact 75 CAUSE 5, unreachable data: the arc stays missing and
    no test pretends to cover it.
    """
    s = _seed()
    existing = _attach_icon(s.feed)
    stale_id = existing.id
    db.session.delete(existing)
    db.session.commit()

    assert File.query.get(stale_id) is None
    assert Feed.query.get(s.feed.id).icon_id is None
