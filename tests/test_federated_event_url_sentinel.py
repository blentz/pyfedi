"""What `Post.new()` stores as an Event's "no url", and what that federates as.

`Post.new()`'s Event branch (`app/models.py`) wrote `''` twice: once as the
branch's own default before it looks for a Link attachment, and once when the
attachment url it found will not parse. Both now write `None`.

Why it matters, and why it is not cosmetic. `post_to_page`
(`app/activitypub/util.py:170`) is the ONLY place in `app/` that distinguishes
the two:

    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or
            post.type == POST_TYPE_EVENT) and post.url is not None:
        activity_data["attachment"] = [{"href": post.url, "type": "Link"}]

`''` is not None, so every Mobilizon event PieFed has ingested has been
federating `{"href": ""}` to its peers -- an attachment claiming a link that is
not there. This is live in production, not a latent hazard.

The EVENT-path audit behind the change. The Article path's equivalent
(`update_post_from_activity`'s Links section, aligned to None earlier on this
branch) was audited over the render path a POST_TYPE_ARTICLE post reaches;
an event renders through different templates, so the audit was redone for them:

- `render_event` in `app/templates/post/post_teaser/_macros.html` and in
  `app/templates/themes/dillo/post/post_teaser/_macros.html` -- the only two
  render_event macros -- do not read `post.url` at all.
- `app/templates/post/_post_full.html` is the full-post view. Every `post.url`
  method call in it (`.endswith`, `.startswith`, `.replace`, `in`) is inside an
  arm gated on `post.type == POST_TYPE_LINK` or `POST_TYPE_VIDEO`, which an
  event never has. Its two remaining reads are truthiness tests
  (`not (post.url and 'youtube.com' in post.url)`), which cannot tell `''` from
  `None`.
- `app/templates/post/post.html` and `app/templates/post/post_edit.html`, the
  other two templates that branch on `POST_TYPE_EVENT`, contain no `post.url`.
- The post-teaser title line reads `{% if post.domain_id and post.url -%}` in
  both themes: truthiness again.
- Python: the only identity comparison against `post.url` anywhere in `app/` is
  `post_to_page`'s, above. `app/api/alpha/views.py:63`,
  `app/nntp/server.py:101-103` and `app/domain/routes.py:150` all read it for
  truth, and the first two are additionally gated on LINK/VIDEO.

So `None` is safe on the EVENT path for the same reason it was on the Article
path, and `''` is wrong for one reason the Article path did not have.

Ordering note: this change is only safe because `update_post_from_activity`'s
Links section no longer initialises `new_url` to a literal `''` for events
(`tests/test_event_post_type_survives_update.py`). With that sentinel still
hard-coded, a freshly ingested event storing `None` would be retyped to a
discussion by its very next Update.
"""

import pytest

from app import db
from app.activitypub.util import create_post, post_to_page
from app.constants import POST_TYPE_EVENT
from app.models import Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

# The keys Post.new()'s Event branch reads unconditionally.
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

# urlparse refuses this: an unbalanced IPv6 bracket raises "Invalid IPv6 URL".
UNPARSEABLE = 'https://[/evil'


@pytest.fixture
def event_ingester(db_session):
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('mobilizon.example'), 'sentinelauthor')
    community = make_community('sentinelcomm')

    def ingest(seq, **object_extra):
        post = create_post(False, community, {
            'id': f'https://mobilizon.example/activities/create/{seq}',
            'type': 'Create',
            'to': ['https://www.w3.org/ns/activitystreams#Public'],
            'object': {
                'id': f'https://mobilizon.example/events/{seq}',
                'type': 'Event',
                'name': 'A community picnic',
                'content': '<p>Bring a blanket and something to share.</p>',
                'mediaType': 'text/html',
                'attributedTo': author.ap_public_url,
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                **EVENT_KEYS,
                **object_extra,
            },
        }, author)
        assert post is not None and post.type == POST_TYPE_EVENT
        return db.session.query(Post).filter_by(
            ap_id=f'https://mobilizon.example/events/{seq}').one()

    return ingest, author, community


def test_an_ingested_event_with_no_link_attachment_stores_none(app, db_session, event_ingester):
    """Asserted against a post that never had a url rather than against the
    literal, so it is the column's own no-url value that is being pinned."""
    ingest, author, community = event_ingester
    never_had_one = make_post(community, author, 'https://mobilizon.example/notes/x',
                              title='no url here')
    assert never_had_one.url is None

    stored = ingest(1)

    assert stored.url is never_had_one.url


def test_an_ingested_event_with_an_unparseable_link_stores_none(app, db_session, event_ingester):
    """The branch's second `''`: an attachment href `url_is_parseable` refuses.
    It has to agree with the first, or the two sentinels diverge again."""
    stored = event_ingester[0](2, attachment=[{'type': 'Link', 'href': UNPARSEABLE}])

    assert stored.url is None
    assert stored.type == POST_TYPE_EVENT


def test_an_event_with_no_url_federates_no_attachment(app, db_session, event_ingester):
    """The consequence, and the reason this is a defect rather than a tidy-up.

    `post_to_page` initialises `attachment` to `[]` and replaces it only when
    `post.url is not None`. With `''` stored, peers received
    `[{'href': '', 'type': 'Link'}]`.
    """
    stored = event_ingester[0](3)

    assert post_to_page(stored)['attachment'] == []


def test_an_event_that_has_a_link_still_federates_it(app, db_session, http_mock, event_ingester):
    """The over-correction guard. A change that stored None unconditionally --
    or that made post_to_page skip events -- would pass all three tests above
    and silently stop federating the events that DO carry a website link.

    The HEAD is is_image_url's, reached from Post.new()'s url handling.
    """
    link = 'https://tickets.example/picnic'
    http_mock.head(link).respond(200, headers={'Content-Type': 'text/html'})

    stored = event_ingester[0](4, attachment=[{'type': 'Link', 'href': link}])

    assert stored.url == link
    assert post_to_page(stored)['attachment'] == [{'href': link, 'type': 'Link'}]
