"""What a refresh takes from a peer's document: the display name, and the one
setting whose guard named a different key than its read.

D1374, the two halves of one round.

FIRST HALF -- the display name at the two refresh sites D1372 did not reach.
`refresh_community_profile_task` and `refresh_feed_profile_task` both held

    community.title = activity_json['name'].strip()

read by hand, while `refresh_user_profile_task`'s equivalent was guarded for
presence and falsiness (and, after D1372, for type). Both tasks end
`except Exception: session.rollback(); raise`, so any failure here leaves the
actor stale for ever -- its name, icon, description, moderator list AND rotated
key all stop being picked up, and every later interaction queues a task that
fails again. Measured:

    name absent                KeyError: 'name'
    None / 5 / [] / {} / True  AttributeError: ... has no attribute 'strip'
    300 characters             DataError: value too long for character
                               varying(256)
    '  Padded  '               ok, title='Padded'

SECOND HALF -- `default_post_type`, which was worse than a crash:

    community.default_post_type = activity_json['defaultPostType'] \\
        if 'default_post_type' in activity_json else 'link'

The guard tests `default_post_type` and the read fetches `defaultPostType`. Two
different keys, so the guard could never be true for the key being fetched --
and this instance PUBLISHES `defaultPostType` (app/activitypub/routes.py:546).
Measured against the refresh of a community whose setting was 'image':

    {'defaultPostType': 'image'}      ok -- and default_post_type='link'
    {'default_post_type': 'image'}    KeyError: 'defaultPostType'
    {}                                ok, 'link' (the intended default arm)
    both keys                         ok, 'image'

So between two PieFed instances every community refresh silently reset a remote
community's default post type, and a peer publishing only the snake_case
spelling aborted the task. The creation branch reads the same key with a
matching guard, which is why nothing noticed: a community arrived with the
right setting and lost it on its first refresh.

WHAT IS DELIBERATELY UNCHANGED. `else 'link'` still resets the setting when the
peer publishes no usable value. That matches the five settings written around it
-- `sensitive`, `postingRestrictedToMods`, `newModsWanted`, `privateMods`,
`questionAnswer` all take their default on a refresh -- and a remote community on
software with no such setting genuinely has no default post type. The title is
the opposite case and is treated as such: it is an identity field with no
meaningful default, so an unusable value leaves what the instance holds.
"""
import pytest

from app import db
from app.activitypub.util import (refresh_community_profile_task,
                                  refresh_feed_profile_task)
from app.models import Community, Feed
from tests.test_ap_refresh_profiles import (PEER, _feed_document, _group_document,
                                            _remote_community, _remote_feed, _serve)

UNUSABLE = [None, 5, [], {}, True, ['Memes'], '', '   ']


def _refresh_community(http_mock, fields=None, omit=()):
    community = _remote_community()
    cid = community.id
    document = _group_document()
    for key in omit:
        document.pop(key, None)
    if fields:
        document.update(fields)
    _serve(http_mock, f'https://{PEER}/c/memes', document)
    refresh_community_profile_task(cid, None)
    db.session.expire_all()
    return db.session.get(Community, cid)


def _refresh_feed(http_mock, fields=None, omit=()):
    feed = _remote_feed()
    fid = feed.id
    document = _feed_document()
    for key in omit:
        document.pop(key, None)
    if fields:
        document.update(fields)
    _serve(http_mock, f'https://{PEER}/f/news', document)
    refresh_feed_profile_task(fid)
    db.session.expire_all()
    return db.session.get(Feed, fid)


# --------------------------------------------------------------------------
# The display name
# --------------------------------------------------------------------------


class TestACommunitysTitle:
    @pytest.mark.parametrize('value', UNUSABLE)
    def test_an_unusable_name_does_not_abort_the_refresh(self, app, db_session,
                                                         http_mock, value):
        assert _refresh_community(http_mock, {'name': value}) is not None

    def test_an_absent_name_does_not_abort_the_refresh(self, app, db_session,
                                                       http_mock):
        """`KeyError: 'name'`, measured. Separate from the parametrised rows
        because an absent key and a present-but-unusable value reached different
        exceptions and could be fixed independently."""
        assert _refresh_community(http_mock, omit=('name',)) is not None

    @pytest.mark.parametrize('value', UNUSABLE)
    def test_the_title_this_instance_holds_is_kept(self, app, db_session,
                                                   http_mock, value):
        """Not blanked, and not a stringified `None` or `5`."""
        assert _refresh_community(http_mock, {'name': value}).title == 'memes'

    def test_a_name_wider_than_the_column_is_cut(self, app, db_session, http_mock):
        """`Community.title` is String(256), and this was
        `DataError: value too long for type character varying(256)` at the task's
        commit -- which rolls back the whole refresh, not just the title."""
        assert _refresh_community(http_mock, {'name': 'M' * 300}).title == 'M' * 256

    def test_a_usable_name_is_applied_and_stripped(self, app, db_session,
                                                   http_mock):
        """The write still happens -- a fix that only ever kept the old title
        would pass every row above."""
        assert _refresh_community(http_mock, {'name': '  Dank Memes  '}).title \
            == 'Dank Memes'

    def test_the_rest_of_the_document_is_still_applied(self, app, db_session,
                                                      http_mock):
        """The point of the fix rather than its mechanism: the task raised at the
        title and never reached the key or the other settings, so a peer
        publishing an unusable name froze everything else about the community."""
        community = _refresh_community(http_mock, {'name': None,
                                                   'sensitive': True,
                                                   'postingRestrictedToMods': True})

        assert community.nsfw is True
        assert community.restricted_to_mods is True
        assert community.public_key == '-----BEGIN PUBLIC KEY-----refreshed'


class TestAFeedsTitle:
    @pytest.mark.parametrize('value', UNUSABLE)
    def test_an_unusable_name_does_not_abort_the_refresh(self, app, db_session,
                                                         http_mock, value):
        assert _refresh_feed(http_mock, {'name': value}) is not None

    def test_an_absent_name_does_not_abort_the_refresh(self, app, db_session,
                                                       http_mock):
        assert _refresh_feed(http_mock, omit=('name',)) is not None

    @pytest.mark.parametrize('value', UNUSABLE)
    def test_the_title_this_instance_holds_is_kept(self, app, db_session,
                                                   http_mock, value):
        assert _refresh_feed(http_mock, {'name': value}).title == 'news'

    def test_a_name_wider_than_the_column_is_cut(self, app, db_session, http_mock):
        assert _refresh_feed(http_mock, {'name': 'N' * 300}).title == 'N' * 256

    def test_a_usable_name_is_applied_and_stripped(self, app, db_session,
                                                   http_mock):
        assert _refresh_feed(http_mock, {'name': '  The News  '}).title == 'The News'


# --------------------------------------------------------------------------
# default_post_type
# --------------------------------------------------------------------------


class TestTheDefaultPostTypeAPeerPublishes:
    """The guard named `default_post_type` and the read named `defaultPostType`."""

    def _with_setting(self, http_mock, fields, held='image'):
        community = _remote_community()
        community.default_post_type = held
        db.session.commit()
        cid = community.id
        document = _group_document()
        document.update(fields)
        _serve(http_mock, f'https://{PEER}/c/memes', document)
        refresh_community_profile_task(cid, None)
        db.session.expire_all()
        return db.session.get(Community, cid)

    def test_the_key_this_instance_publishes_is_read(self, app, db_session,
                                                     http_mock):
        """D1374's measured defect: `ok` and `default_post_type='link'`, for the
        one spelling app/activitypub/routes.py:546 actually sends. Every refresh
        between two PieFed instances threw the remote community's setting away."""
        assert self._with_setting(http_mock,
                                  {'defaultPostType': 'image'}).default_post_type \
            == 'image'

    def test_the_snake_case_spelling_no_longer_aborts_the_refresh(
            self, app, db_session, http_mock):
        """`KeyError: 'defaultPostType'`, measured -- the guard was true and the
        read was of a key that was not there. The value is ignored now, which is
        what an unrecognised key should be."""
        community = self._with_setting(http_mock,
                                       {'default_post_type': 'video'})

        assert community is not None
        assert community.default_post_type == 'link'

    def test_a_document_publishing_neither_takes_the_default(self, app, db_session,
                                                             http_mock):
        """Unchanged behaviour, asserted because the fix rewrote the expression:
        `else 'link'` matches the five settings written around it, and a remote
        community on software with no such setting has no default post type."""
        assert self._with_setting(http_mock, {}).default_post_type == 'link'

    @pytest.mark.parametrize('value', [None, 5, [], {}, True, ''])
    def test_an_unusable_value_takes_the_default(self, app, db_session, http_mock,
                                                 value):
        assert self._with_setting(
            http_mock, {'defaultPostType': value}).default_post_type == 'link'

    def test_a_value_wider_than_the_column_is_cut(self, app, db_session,
                                                 http_mock):
        """String(15). The column is narrow enough that a peer sending a long
        string was a DataError rather than a silly value."""
        community = self._with_setting(http_mock,
                                      {'defaultPostType': 'i' * 40})

        assert community.default_post_type == 'i' * 15

    @pytest.mark.parametrize('value', ['link', 'image', 'discussion', 'video',
                                       'poll'])
    def test_each_type_the_form_offers_survives_a_refresh(self, app, db_session,
                                                          http_mock, value):
        """The values `app/community/forms.py:157` offers. A refresh that mangled
        any of them would show as a community whose compose box changed type."""
        assert self._with_setting(
            http_mock, {'defaultPostType': value}).default_post_type == value


class TestCreationReadsItTheSameWay:
    """The creation branch had the right key and the same type and width holes.
    Creation and refresh disagreeing about one column is how this survived: a
    community arrived with the correct setting and lost it on first refresh."""

    @pytest.mark.parametrize('value', [None, 5, [], {}, True, '', 'i' * 40])
    def test_an_unusable_value_does_not_refuse_the_community(self, app, db_session,
                                                             value):
        from app.activitypub.util import actor_json_to_model
        from tests.factories import peer_actor_json, peer_instance

        peer_instance(PEER)
        document = peer_actor_json('Group', name='memes', server=PEER,
                                   fields={'defaultPostType': value})

        community = actor_json_to_model(document, '!memes', PEER)

        assert community is not None
        assert community.default_post_type in ('link', 'i' * 15)

    def test_a_usable_value_is_kept(self, app, db_session):
        from app.activitypub.util import actor_json_to_model
        from tests.factories import peer_actor_json, peer_instance

        peer_instance(PEER)
        document = peer_actor_json('Group', name='memes', server=PEER,
                                   fields={'defaultPostType': 'image'})

        assert actor_json_to_model(document, '!memes',
                                   PEER).default_post_type == 'image'
