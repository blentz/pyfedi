"""A peer's `image` key, at the four sites that turn it into a File.

`Post.new` (twice) in `app/models.py`, and `update_post_from_activity` and
`create_post`'s thumbnail fallback in `app/activitypub/util.py`.

D1352. Two of the four read `request_json['object']['image']['url']` with no guard
at all. The other two were guarded by `'url' in request_json['object']['image']`,
which is a guard that subscripts what it guards: on a bare-string `image` that is a
SUBSTRING test, so a url containing the letters "url" passed it and the subscript
below raised anyway. Measured through `Post.new`:

    image='https://peer.test/pic.png'            TypeError: string indices must
                                                 be integers, not 'str'
    image='https://peer.test/url.png'            the same, past the 'url' in ...
                                                 guard the other two sites use
    image={}                                     KeyError: 'url'
    image=[]                                     TypeError: list indices must be
                                                 integers or slices, not str
    image=[{'url': 'https://peer.test/pic.png'}] the same
    image=5                                      TypeError: 'int' object is not
                                                 subscriptable
    image=None                                   TypeError: 'NoneType' object is
                                                 not subscriptable
    image={'url': 5}                             stored, source_url='5'

Only `{'url': '<a string>'}` worked, and `create_post`'s `except Exception` then
dropped the peer's whole post. A bare-string `image` is valid ActivityPub and is
the commonest spelling outside Lemmy, so this was most of the fediverse's link
posts.

`image_url_from` is the one reading of `icon` and `image` -- D1325's helper, which
D1341 extended to `Post.new`'s Video branch. This round is the third copy of the
same key it did not yet cover.
"""
from datetime import datetime

import pytest
from flask import current_app, g

from app import db
from app.models import File, Post, Site, image_url_from

AUTHOR = 'https://remote.test/u/someone'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
ARTICLE = 'https://peer.test/article'
PICTURE = 'https://peer.test/pic.png'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    from tests.factories import make_community, make_community_member, make_user

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('imageland')
    author = make_user(api_baseline.instance_remote, 'imager')
    author.ap_id = 'imager@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', '')
    return SimpleNamespace(community=community, author=author,
                           baseline=api_baseline)


@pytest.fixture
def link_target(http_mock):
    """The HEAD `is_image_url` makes to classify the link a Page points at.

    The opengraph GET is NOT registered here: it only happens when no image was
    recognised, and `http_mock` asserts every route it is given is used, so a test
    that DOES recognise an image would fail on the unused route. `opengraph` below
    adds it for the tests that reach the fallback.
    """
    http_mock.head(ARTICLE).respond(200, headers={'Content-Type': 'text/html'})
    return ARTICLE


@pytest.fixture
def opengraph(http_mock):
    """The fallback fetch `Post.new` makes when the peer gave it no usable image."""
    http_mock.get(ARTICLE).respond(200, headers={'Content-Type': 'text/html'},
                                   text='<html><head></head><body></body></html>')
    return ARTICLE


def a_link_post(env, image, counter=[0]):
    counter[0] += 1
    document = {'id': f'https://remote.test/p/{counter[0]}', 'type': 'Page',
                'name': 'a post', 'attributedTo': AUTHOR, 'to': [PUBLIC],
                'published': '2026-01-01T00:00:00Z',
                'attachment': [{'type': 'Link', 'href': ARTICLE}]}
    if image is not ...:
        document['image'] = image
    return Post.new(env.author, env.community,
                    {'id': 'https://remote.test/c/1', 'type': 'Create',
                     'to': [PUBLIC], 'object': document})


class TestTheShapesThatUsedToLoseThePost:
    """Every one of these raised out of `Post.new` before the repair. The post has
    to arrive: an image this instance cannot read is not a reason to drop a peer's
    article."""

    @pytest.mark.parametrize('image', [
        {},
        {'url': None},
        {'url': 5},
        {'url': ''},
        [],
        [5],
        [None],
        5,
        None,
        '',
        True,
    ])
    def test_the_post_arrives_without_an_image(self, env, link_target, opengraph,
                                               image):
        post = a_link_post(env, image)

        assert post is not None
        assert post.url == ARTICLE
        assert post.image is None

    def test_a_bare_string_image_is_used(self, env, link_target):
        """The shape that lost the most posts, and it is a url: it should be
        STORED, not merely survived."""
        post = a_link_post(env, PICTURE)

        assert post.image is not None
        assert post.image.source_url == PICTURE

    def test_a_bare_string_containing_the_word_url(self, env, link_target):
        """`'url' in image` is a substring test on a string. This is the value that
        passed the guard the other two sites used and raised one line later."""
        post = a_link_post(env, 'https://peer.test/url.png')

        assert post.image is not None
        assert post.image.source_url == 'https://peer.test/url.png'

    def test_an_object_with_a_url_still_works(self, env, link_target):
        post = a_link_post(env, {'url': PICTURE})

        assert post.image.source_url == PICTURE

    def test_a_list_of_objects_uses_the_first(self, env, link_target):
        """`image_url_from` takes the FIRST entry of an image list and the LAST of
        an icon list, which is the convention each key is served under."""
        post = a_link_post(env, [{'url': PICTURE},
                                 {'url': 'https://peer.test/second.png'}])

        assert post.image.source_url == PICTURE

    def test_no_image_key_at_all(self, env, link_target, opengraph):
        post = a_link_post(env, ...)

        assert post is not None
        assert post.image is None

    def test_a_numeric_url_is_not_stored_as_a_string(self, env, link_target,
                                                     opengraph):
        """`{'url': 5}` used to be stored, leaving `source_url = '5'` -- a column
        holding a value nothing can fetch and `delete_from_disk` cannot read."""
        post = a_link_post(env, {'url': 5})

        assert File.query.filter_by(source_url='5').count() == 0
        assert post.image is None


class TestTheHelperReadsBothKeysOneWay:
    """`image_url_from` directly, for the `image` end of it. The `icon` end and the
    prefer_last argument belong to D1325 and D1341 and are pinned with those."""

    @pytest.mark.parametrize('value, expected', [
        (PICTURE, PICTURE),
        ({'url': PICTURE}, PICTURE),
        ([{'url': PICTURE}], PICTURE),
        ([PICTURE], PICTURE),
        ({}, None),
        ({'url': ''}, None),
        ({'url': 5}, None),
        ({'url': {'href': PICTURE}}, None),
        ([], None),
        ([5], None),
        ([{}], None),
        ('', None),
        (5, None),
        (None, None),
        (True, None),
    ])
    def test_what_it_reads(self, value, expected):
        assert image_url_from(value) == expected


class TestAnEventEdit:
    """`update_post_from_activity`'s Event branch, which is where the fourth
    `image` read lives -- and D1353: the same thirteen keys round 150 repaired in
    `Post.new`'s Event branch (D1339) were still read outright here.

    Every one was `request_json['object'][...]`, so an Event edit missing ANY key
    was a KeyError, and nine of the thirteen are optional in the vocabulary. The
    two timestamps went into `datetime.fromisoformat` directly, a ValueError for
    anything that is not a date. Both raise out of the inbox, so the edit was lost.
    """

    def an_event_post(self, env):
        from app.constants import POST_TYPE_EVENT
        from app.models import Event
        from tests.factories import make_post

        post = make_post(env.community, env.author,
                         ap_id='https://remote.test/p/event')
        post.type = POST_TYPE_EVENT
        post.url = None
        db.session.add(Event(post_id=post.id,
                             start=datetime(2026, 5, 1, 10, 0),
                             end=datetime(2026, 5, 1, 12, 0),
                             timezone='Europe/London', max_attendees=50,
                             participant_count=3, online_link='https://old.test/call',
                             join_mode='free', external_participation_url='',
                             anonymous_participation=False, online=True,
                             buy_tickets_link='', event_fee_currency='GBP',
                             event_fee_amount=5.0))
        db.session.commit()
        return post, Event.query.filter_by(post_id=post.id).one()

    def an_update(self, **fields):
        document = {'id': 'https://remote.test/p/event', 'type': 'Event',
                    'name': 'an edited event', 'attributedTo': AUTHOR,
                    'to': [PUBLIC], 'content': '<p>new body</p>'}
        document.update(fields)
        return {'id': 'https://remote.test/u/1', 'type': 'Update',
                'to': [PUBLIC], 'object': document}

    def test_an_update_with_no_event_keys_at_all_is_applied(self, env):
        """The KeyError, thirteen times over: a peer that sends only what it
        changed used to lose the whole edit."""
        from app.activitypub.util import update_post_from_activity

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update())

        db.session.refresh(post)
        assert post.title == 'an edited event'

    def test_the_details_it_did_not_send_are_kept(self, env):
        """An Update carries the whole object, so a key this instance cannot read
        is not the peer clearing the field. Guessing the other way erases an
        event's details silently."""
        from app.activitypub.util import update_post_from_activity
        from app.models import Event

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update())

        fresh = Event.query.filter_by(post_id=post.id).one()
        assert fresh.start == datetime(2026, 5, 1, 10, 0)
        assert fresh.timezone == 'Europe/London'
        assert fresh.max_attendees == 50
        assert fresh.online_link == 'https://old.test/call'
        assert fresh.event_fee_amount == 5.0

    @pytest.mark.parametrize('start', ['whenever', '', 5, [], {}, None])
    def test_a_start_time_that_is_not_a_date_leaves_the_old_one(self, env, start):
        """`datetime.fromisoformat('whenever')` is a ValueError out of the inbox."""
        from app.activitypub.util import update_post_from_activity
        from app.models import Event

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(startTime=start))

        db.session.refresh(post)
        assert post.title == 'an edited event'
        assert Event.query.filter_by(post_id=post.id).one().start == \
            datetime(2026, 5, 1, 10, 0)

    def test_a_readable_start_time_is_applied_and_converted(self, env):
        from app.activitypub.util import update_post_from_activity
        from app.models import Event

        post, event = self.an_event_post(env)

        update_post_from_activity(post,
                                  self.an_update(startTime='2026-06-01T09:00:00+02:00',
                                                 endTime='2026-06-01T11:00:00+02:00'))

        fresh = Event.query.filter_by(post_id=post.id).one()
        assert fresh.start == datetime(2026, 6, 1, 7, 0)
        assert fresh.end == datetime(2026, 6, 1, 9, 0)

    @pytest.mark.parametrize('field, sent, attribute, expected', [
        ('timezone', 'Europe/Paris', 'timezone', 'Europe/Paris'),
        ('maximumAttendeeCapacity', 99, 'max_attendees', 99),
        ('participantCount', 7, 'participant_count', 7),
        ('onlineLink', 'https://new.test/call', 'online_link', 'https://new.test/call'),
        ('joinMode', 'restricted', 'join_mode', 'restricted'),
        ('anonymousParticipation', True, 'anonymous_participation', True),
        ('isOnline', False, 'online', False),
        ('feeCurrency', 'EUR', 'event_fee_currency', 'EUR'),
        ('feeAmount', 12.5, 'event_fee_amount', 12.5),
    ])
    def test_a_field_the_peer_does_send_is_applied(self, env, field, sent,
                                                  attribute, expected):
        from app.activitypub.util import update_post_from_activity
        from app.models import Event

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(**{field: sent}))

        fresh = Event.query.filter_by(post_id=post.id).one()
        assert getattr(fresh, attribute) == expected

    @pytest.mark.parametrize('field, sent', [
        ('maximumAttendeeCapacity', 'lots'),
        ('participantCount', 'many'),
        ('feeAmount', 'free'),
        ('timezone', 5),
    ])
    def test_a_field_this_instance_cannot_read_does_not_raise(self, env, field, sent):
        from app.activitypub.util import update_post_from_activity

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(**{field: sent}))

        db.session.refresh(post)
        assert post.title == 'an edited event'

    def test_a_timezone_longer_than_the_column_is_truncated(self, env):
        """`_as_text(..., 30)`: `Event.timezone` is String(30), and a longer value
        was a DataError at commit that lost the edit."""
        from app.activitypub.util import update_post_from_activity
        from app.models import Event

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(timezone='x' * 200))

        assert len(Event.query.filter_by(post_id=post.id).one().timezone) == 30

    @pytest.mark.parametrize('image', ['https://peer.test/url.png', 5, [], {},
                                       None, [{'url': PICTURE}]])
    def test_the_edit_is_applied_whatever_the_image_is(self, env, image):
        from app.activitypub.util import update_post_from_activity

        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(image=image))

        db.session.refresh(post)
        assert post.title == 'an edited event'

    def test_a_bare_string_image_becomes_the_events_image(self, env, monkeypatch):
        """The fourth site, and the one whose guard was a substring test."""
        from app.activitypub.util import update_post_from_activity

        monkeypatch.setattr('app.activitypub.util.get_setting',
                            lambda name, default=None: False)
        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(image=PICTURE))

        db.session.refresh(post)
        assert post.image is not None
        assert post.image.source_url == PICTURE

    def test_an_unreadable_image_leaves_the_event_without_one(self, env, monkeypatch):
        from app.activitypub.util import update_post_from_activity

        monkeypatch.setattr('app.activitypub.util.get_setting',
                            lambda name, default=None: False)
        post, event = self.an_event_post(env)

        update_post_from_activity(post, self.an_update(image={'url': 5}))

        db.session.refresh(post)
        assert post.image is None
