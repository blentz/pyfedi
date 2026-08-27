"""An Event must not be silently retyped to a discussion by an unrelated Update.

The diagnosis, because "why" decides what the fix may touch.

`update_post_from_activity`'s Links section (`app/activitypub/util.py`) opens
with

    old_url = post.url
    new_url = '' if post.type == POST_TYPE_EVENT else None

and later runs the whole "the url changed" arm whenever `old_url != new_url`.
The `''` is deliberate -- its own comment says events have no url and it exists
"to avoid triggering the 'this url has changed' code". It works only because
`Post.new()`'s Event branch (`app/models.py`) writes `''` as ITS "no url" value.

Two writers disagree with that. `app/shared/post.py:613` stores `None` for a
locally created event that has a banner image ("Events don't have URLs when they
have banner images"), and aligning `Post.new()`'s Event branch to `None` -- the
column's own no-url value, and what `post_to_page` needs so it stops federating
`{"href": ""}` -- makes every federated event agree with it. Either way
`old_url` is `None` while `new_url` is `''`, `None != ''` is true, the arm runs,
`new_url` is falsy, and its `else` sets `post.type = POST_TYPE_ARTICLE` and
`post.image_id = None`. An Event that merely had its start time corrected comes
back a discussion with its banner deleted.

So this is NOT a defect in the type derivation. The type derivation
(`is_image_url` -> IMAGE, `is_video_hosting_site` -> VIDEO, else LINK, no
attachment -> ARTICLE) is only reached because a sentinel mismatch made
change-detection fire on an unchanged url. The fix is to make the "no change"
comparison true whatever the stored sentinel is:

    new_url = old_url if post.type == POST_TYPE_EVENT else None

`old_url` is `post.url` captured two lines above and nothing writes `post.url`
in between, so for an Event with no Link attachment this compares a value to
itself -- true for `None`, for `''`, and for a legacy row holding either. That
is what lets `Post.new()` switch to `None` with no migration for rows already
holding `''`.

REPORTED AND NOT FIXED, because it is a type-derivation question and needs
product input: an Event whose Update carries a *genuinely different* Link
attachment does enter the arm legitimately, and the arm then overwrites
`post.type` with LINK/IMAGE/VIDEO -- there is no `POST_TYPE_EVENT` case in it at
all. `Post.new()` keeps POST_TYPE_EVENT while setting `post.url` from the same
attachment, so the create and update paths disagree about what an event with a
website link is. `test_a_changed_link_attachment_still_retypes_the_event` pins
that as observed behaviour, deliberately: it is a record of the defect, not a
statement that the behaviour is wanted.
"""

import pytest

from app import db
from app.activitypub.util import create_post, update_post_from_activity
from app.constants import POST_TYPE_EVENT, POST_TYPE_LINK
from app.models import File, Post
from app.utils import set_setting
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

# The keys Post.new()'s Event branch and update_post_from_activity's Event
# branch both read unconditionally.
EVENT_KEYS = {
    'startTime': '2030-01-01T10:00:00',
    'endTime': '2030-01-01T12:00:00',
    'timezone': 'Europe/London',
    'maximumAttendeeCapacity': 50,
    'participantCount': 0,
    'onlineLink': '',
    'joinMode': 'free',
    'externalParticipationUrl': '',
    'anonymousParticipation': False,
    'isOnline': False,
    'buyTicketsLink': '',
    'feeCurrency': 'GBP',
    'feeAmount': 0,
    'location': {'type': 'Place', 'name': 'somewhere'},
}

AP_ID = 'https://mobilizon.example/events/1'


def _event_object(**extra):
    return {
        'id': AP_ID,
        'type': 'Event',
        'name': 'A community picnic',
        'content': '<p>Bring a blanket and something to share.</p>',
        'mediaType': 'text/html',
        'attributedTo': 'https://mobilizon.example/users/a',
        'to': ['https://www.w3.org/ns/activitystreams#Public'],
        **EVENT_KEYS,
        **extra,
    }


@pytest.fixture
def federated_event(db_session):
    """An EVENT post built through the real federated Create path.

    Going through `create_post` rather than assembling a Post by hand is what
    makes the Event row, the post type and the ap_id production's own shape --
    `update_post_from_activity`'s Event branch reads the Event row and returns
    early if it is missing, so a hand-built post would exercise a different path.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('mobilizon.example'), 'eventauthor')
    community = make_community('eventcomm')
    post = create_post(False, community, {
        'id': 'https://mobilizon.example/activities/create/1',
        'type': 'Create',
        'to': ['https://www.w3.org/ns/activitystreams#Public'],
        'object': _event_object(),
    }, author)
    assert post is not None and post.type == POST_TYPE_EVENT
    return post


def _update(post, seq=1, **object_extra):
    """An Update correcting the event's start time and nothing else."""
    update_post_from_activity(post, {
        'id': f'https://mobilizon.example/activities/update/{seq}',
        'type': 'Update',
        'object': _event_object(startTime='2030-01-01T11:00:00', **object_extra),
    })


def _stored():
    return db.session.query(Post).filter_by(ap_id=AP_ID).one()


def test_an_event_with_a_null_url_keeps_its_type(app, db_session, federated_event):
    """The reported defect. `None` is what `app/shared/post.py:613` stores for a
    locally created event with a banner image, and what the federated create
    path stores once its Event branch is aligned to the column's own no-url
    value."""
    federated_event.url = None
    db.session.commit()

    _update(federated_event)

    stored = _stored()
    assert stored.type == POST_TYPE_EVENT, 'the Update retyped the event'
    assert stored.url is None


def test_an_event_with_a_null_url_keeps_the_banner_its_update_supplies(
        app, db_session, federated_event):
    """The same arm clears `post.image_id` on its way past, so the type
    assertion alone understates the damage.

    The banner has to arrive IN the Update. The Event branch a few lines above
    the Links section replaces `post.image` from `request_json['object']['image']`
    and clears it when the key is absent -- an Update with no image legitimately
    has no banner, and asserting on a pre-existing image would be measuring that
    branch rather than this one. So the Update supplies one, the Event branch
    stores it, and pre-fix the Links section's `else` wipes it again three
    statements later.

    `cache_remote_images_locally` is turned off because `make_image_sizes`
    otherwise fetches the banner over the network.
    """
    set_setting('cache_remote_images_locally', False)
    banner = 'https://mobilizon.example/banner.png'
    federated_event.url = None
    db.session.commit()

    _update(federated_event, image={'url': banner})

    stored = _stored()
    assert stored.image_id is not None, 'the Update supplied a banner and it was wiped'
    assert db.session.query(File).filter_by(id=stored.image_id).one().source_url == banner


def test_a_legacy_event_holding_the_empty_string_still_keeps_its_type(
        app, db_session, federated_event):
    """No migration accompanies the `Post.new()` change, so rows written before
    it still hold `''`. Comparing `old_url` to itself tolerates both sentinels;
    a fix that merely swapped the literal `''` for a literal `None` would pass
    the test above and fail this one, corrupting every event already stored."""
    federated_event.url = ''
    db.session.commit()

    _update(federated_event)

    stored = _stored()
    assert stored.type == POST_TYPE_EVENT
    assert stored.url == ''


def test_an_event_whose_link_attachment_is_unchanged_keeps_its_type(
        app, db_session, federated_event):
    """An event CAN carry a url: `Post.new()` reads the first Link attachment
    into `post.url` and keeps POST_TYPE_EVENT. Re-sending the same attachment
    must be a no-op, which a fix that hard-coded `new_url = None` for events
    would break -- it would read as "the url was removed"."""
    link = 'https://mobilizon.example/tickets'
    federated_event.url = link
    db.session.commit()

    _update(federated_event, attachment=[{'type': 'Link', 'href': link}])

    stored = _stored()
    assert stored.type == POST_TYPE_EVENT
    assert stored.url == link


def test_a_changed_link_attachment_still_retypes_the_event(
        app, db_session, http_mock, federated_event):
    """PINS A DEFECT, deliberately -- this is a record, not a requirement.

    The "url changed" arm has no POST_TYPE_EVENT case: it derives the type from
    the url's shape and overwrites whatever was there. So an event that
    genuinely changes its website link is retyped to LINK, while `Post.new()`
    handed the same attachment keeps POST_TYPE_EVENT. Which of the two is right
    is a product question about what an event with a website link is, and
    guessing at type derivation is out of scope here. The url itself is stored
    correctly, which is what this asserts first -- a fix for the sentinel that
    froze event urls would fail that half.
    """
    new_link = 'https://mobilizon.example/new-tickets'
    # is_image_url's HEAD, which the arm performs before choosing a type. The
    # opengraph GET that follows it needs no route: opengraph_parse swallows the
    # failure and returns None (app/utils.py:2929).
    http_mock.head(new_link).respond(200, headers={'Content-Type': 'text/html'})
    federated_event.url = 'https://mobilizon.example/tickets'
    db.session.commit()

    _update(federated_event, attachment=[{'type': 'Link', 'href': new_link}])

    stored = _stored()
    assert stored.url == new_link
    assert stored.type == POST_TYPE_LINK, 'see this test name: the retyping is the defect'
