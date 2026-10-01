"""D1403: a peer's Event link rendered as an href, with no scheme checked.

An Event carries three URLs a peer supplies, and all three were read with `_as_text` --
which checks the value is a string and trims it to the column's width, and says nothing
about its scheme. One of them is rendered as a link (`post/_post_full.html:219`):

    <a href="{{ event.online_link }}" target="_blank" rel="nofollow ugc">
        {{ event.online_link }} <span class="fe fe-external"></span>
    </a>

so a peer sending `onlineLink: "javascript:alert(document.domain)"` got a clickable
`javascript:` href on the post page. Measured straight out of `Post.new`:

    PROBE  stored online_link:                'javascript:alert(document.domain)'
           stored external_participation_url: 'javascript:alert(2)'
           stored buy_tickets_link:           'javascript:alert(3)'

`rel="nofollow ugc"` does not stop a scheme from executing, and `target="_blank"` opens
it in a new tab -- it is a stored XSS needing one click, the same class as D1381 (a
stored XSS in the feed listing) and D1373 (`?next=javascript:` on the passkey flow).
The other two fields are stored unrendered today; a template linking them later would
have inherited the hole, which is why all three are fixed rather than only the one.

THE TWO PRODUCERS DISAGREED, which is this campaign's most common shape. The LOCAL form
requires a scheme -- `CreateEventForm.online_link` carries
`Regexp(r'^https?://', message='URLs need to start with "http://"" or "https://"')` and
round 207 gave it `validate_online_link` on top -- while the federated path read the
same field through `_as_text`. `_as_url` is now the rule for both, at all six ingest
sites: three in `Post.new` and three in `update_post_from_activity`.

WHY A BAD URL IS DROPPED RATHER THAN REFUSED. The rest of the event is still worth
ingesting, and None is exactly what these columns hold for an event that named no link
at all -- so the template's `{% if event.online %}` block renders an empty href rather
than a dangerous one, and nothing downstream meets a shape it has not already seen.

WHAT THE SWEEP THAT FOUND IT LOOKED AT. Every `href`/`src`/`action` in `app/templates`
whose value is a bare `{{ expression }}` rather than `url_for(...)`: 343 sites, grouped
by expression. Most are `community.icon_image()`, `file.view_url()` and similar --
values this instance computes. `{{ field.text }}` (a user's extra profile field, also
peer-supplied) IS guarded, by `{% if field.text.startswith('http') %}` in
`user/show_profile.html:181`. `{{ event.online_link }}` was the one with no guard at
either end.
"""
import contextlib

import pytest
from flask import g

from app import db
from app.models import Event, Post, Site, _as_url
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'

# Schemes a browser will act on from an href, and shapes that are not links at all.
# `javascript:` is the one that executes; the rest are here because a guard narrowed to
# that single word would pass them.
NOT_HTTP_URLS = [
    'javascript:alert(document.domain)',
    'JavaScript:alert(1)',
    'javascript&colon;alert(1)',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'ftp://example.test/x',
    '//evil.example/x',
    '/relative/path',
    'example.test/no-scheme',
    'httpjavascript:alert(1)',
    # A valid scheme present but not at the start, which is what separates
    # `startswith` from `in`.
    "javascript:fetch('https://evil.example/steal')",
    'data:text/html,<a href="https://ok.example">x</a>',
    ' javascript:alert(1)',
    '',
]

HTTP_URLS = [
    'https://example.test/room',
    'http://example.test/room',
    'HTTPS://EXAMPLE.TEST/ROOM',
    'https://example.test/room?a=b#c',
]


class _RedisLockOnlyDouble:
    """`update_post_from_activity` takes a redis lock around the update. Only `.lock()`
    is needed here, and tests/test_ap_update_pair.py documents the same double."""

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def env(app, db_session):
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    db.session.commit()
    local = make_instance(HOST, software='piefed')
    make_user(local, 'founder', local=True)
    peer = make_instance(PEER)
    author = make_user(peer, 'author')
    author.ap_profile_id = f'https://{PEER}/u/author'
    community = make_community('general')
    db.session.commit()
    return SimpleNamespace(author=author, community=community, peer=peer)


def _event_activity(env, **object_fields):
    fields = {'id': f'https://{PEER}/events/1', 'type': 'Event',
              'name': 'An event', 'attributedTo': env.author.ap_profile_id,
              'audience': env.community.ap_profile_id,
              'startTime': '2050-01-01T00:00:00Z', 'isOnline': True}
    fields.update(object_fields)
    return {'id': f'https://{PEER}/activities/1', 'type': 'Create',
            'actor': env.author.ap_profile_id, 'object': fields}


def _ingested(env, **object_fields):
    post = Post.new(env.author, env.community, _event_activity(env, **object_fields))
    db.session.commit()
    return Event.query.filter_by(post_id=post.id).first()


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


class TestTheCoercion:
    @pytest.mark.parametrize('value', HTTP_URLS)
    def test_an_http_url_is_kept(self, value):
        assert _as_url(value) == value

    @pytest.mark.parametrize('value', NOT_HTTP_URLS)
    def test_anything_a_browser_would_not_fetch_over_http_is_dropped(self, value):
        assert _as_url(value) is None

    @pytest.mark.parametrize('value', [None, 42, True, ['https://example.test'],
                                       {'href': 'https://example.test'}])
    def test_a_value_that_is_not_a_string_is_dropped(self, value):
        """Inherited from `_as_text`, and asserted here so a rewrite of `_as_url` that
        stopped delegating still refuses these."""
        assert _as_url(value) is None

    def test_the_column_width_is_still_applied(self):
        """`_as_url` passes `limit` through to `_as_text`, so a peer choosing a url
        longer than the column cannot turn the ingest into a DataError at commit."""
        long_url = 'https://example.test/' + 'a' * 5000

        assert len(_as_url(long_url, 1024)) == 1024

    def test_the_scheme_is_checked_after_trimming(self):
        """Order matters only one way round: trimming cannot turn a non-url into a url,
        but it could turn a url into a prefix of one -- which still starts with the
        scheme, so it is kept. Stated because a reader might expect the opposite."""
        assert _as_url('https://example.test/x', 10) == 'https://ex'


# --------------------------------------------------------------------------
# Post.new, the first ingest path
# --------------------------------------------------------------------------


class TestIngestingANewEvent:
    @pytest.mark.parametrize('value', NOT_HTTP_URLS)
    def test_an_online_link_that_is_not_an_http_url_is_dropped(self, env, value):
        """The defect. `javascript:alert(document.domain)` was stored verbatim and
        rendered as a clickable href."""
        event = _ingested(env, onlineLink=value)

        assert event is not None
        assert event.online_link is None

    @pytest.mark.parametrize('value', NOT_HTTP_URLS)
    def test_the_other_two_links_are_dropped_too(self, env, value):
        """Stored unrendered today. Fixed with the one that is rendered, because a
        template linking them later would inherit the hole and nothing would say so."""
        event = _ingested(env, externalParticipationUrl=value,
                          buyTicketsLink=value)

        assert event.external_participation_url is None
        assert event.buy_tickets_link is None

    def test_a_real_link_is_kept(self, env):
        """The control. A fix that dropped every link would pass every row above and
        break online events."""
        event = _ingested(env, onlineLink='https://meet.example/room',
                          externalParticipationUrl='https://tickets.example/e/1',
                          buyTicketsLink='http://shop.example/t/1')

        assert event.online_link == 'https://meet.example/room'
        assert event.external_participation_url == 'https://tickets.example/e/1'
        assert event.buy_tickets_link == 'http://shop.example/t/1'

    def test_the_rest_of_the_event_still_arrives(self, env):
        """A bad link is dropped, not refused: the event is still worth having. The
        `startTime` is what makes the post an Event at all, so this row also proves the
        branch ran rather than bailing out."""
        from app import constants
        event = _ingested(env, onlineLink='javascript:alert(1)',
                          timezone='Europe/London', participantCount=7)

        assert event is not None
        assert event.online_link is None
        assert event.timezone == 'Europe/London'
        assert event.participant_count == 7
        assert db.session.get(Post, event.post_id).type == constants.POST_TYPE_EVENT

    def test_an_event_naming_no_link_is_unchanged(self, env):
        """None is what the column already held for an event with no link, which is
        why dropping a bad one introduces no new state downstream."""
        event = _ingested(env)

        assert event.online_link is None


# --------------------------------------------------------------------------
# update_post_from_activity, the second
# --------------------------------------------------------------------------


class TestUpdatingAnEvent:
    @pytest.fixture
    def existing(self, env, redis_lock_only_double):
        """An event already ingested with a good link, so an Update carrying a bad one
        has something to overwrite -- the failure worth catching is a stored-good value
        being replaced by a stored-dangerous one."""
        event = _ingested(env, onlineLink='https://meet.example/room',
                          buyTicketsLink='https://shop.example/t/1',
                          externalParticipationUrl='https://tickets.example/e/1')
        return db.session.get(Post, event.post_id), event

    @pytest.mark.parametrize('value', NOT_HTTP_URLS)
    def test_an_update_cannot_replace_a_good_link_with_a_bad_one(self, env,
                                                                existing, value):
        from app.activitypub.util import update_post_from_activity
        post, event = existing

        update_post_from_activity(post, {'object': {
            'id': post.ap_id, 'type': 'Event', 'name': 'An event',
            'startTime': '2050-01-01T00:00:00Z', 'isOnline': True,
            'onlineLink': value, 'buyTicketsLink': value,
            'externalParticipationUrl': value}})

        refreshed = Event.query.filter_by(post_id=post.id).one()
        assert refreshed.online_link is None
        assert refreshed.buy_tickets_link is None
        assert refreshed.external_participation_url is None

    def test_an_update_can_still_change_a_link(self, env, existing):
        """The control for the update path."""
        from app.activitypub.util import update_post_from_activity
        post, event = existing

        update_post_from_activity(post, {'object': {
            'id': post.ap_id, 'type': 'Event', 'name': 'An event',
            'startTime': '2050-01-01T00:00:00Z', 'isOnline': True,
            'onlineLink': 'https://meet.example/other'}})

        assert Event.query.filter_by(post_id=post.id).one().online_link == \
            'https://meet.example/other'

    def test_an_update_that_omits_a_link_leaves_it_alone(self, env, existing):
        """Each field is behind `if '<key>' in event_json:`, so an Update that says
        nothing about a link must not clear one."""
        from app.activitypub.util import update_post_from_activity
        post, event = existing

        update_post_from_activity(post, {'object': {
            'id': post.ap_id, 'type': 'Event', 'name': 'An event',
            'startTime': '2050-01-01T00:00:00Z', 'isOnline': True}})

        assert Event.query.filter_by(post_id=post.id).one().online_link == \
            'https://meet.example/room'


class TestTheMoreInfoLink:
    """R223, fixed (owner ruling): an event's 'More info' link travels as an
    extra Link attachment named 'More info', and is read back from the same --
    into Event.more_info_url, not into the post's url, scheme-checked like the
    event's other links."""

    MORE_INFO = {'type': 'Link', 'href': 'https://info.example/e', 'name': 'More info'}

    def test_a_new_event_stores_it_and_keeps_its_url_separate(self, env, http_mock):
        http_mock.head('https://site.example/e').respond(200, headers={'Content-Type': 'text/html'})
        event = _ingested(env, attachment=[self.MORE_INFO,
                                           {'type': 'Link', 'href': 'https://site.example/e'}])

        assert event.more_info_url == 'https://info.example/e'
        assert db.session.get(Post, event.post_id).url == 'https://site.example/e'

    def test_a_dangerous_one_is_dropped(self, env):
        event = _ingested(env, attachment=[dict(self.MORE_INFO, href='javascript:alert(1)')])

        assert event.more_info_url is None

    def test_an_update_changes_it_and_one_without_it_clears_it(self, env, redis_lock_only_double):
        from app.activitypub.util import update_post_from_activity
        event = _ingested(env, attachment=[self.MORE_INFO])
        post = db.session.get(Post, event.post_id)
        update = {'id': post.ap_id, 'type': 'Event', 'name': 'An event',
                  'startTime': '2050-01-01T00:00:00Z', 'isOnline': True}

        update_post_from_activity(post, {'object': dict(update, attachment=[
            dict(self.MORE_INFO, href='https://info.example/other')])})
        assert Event.query.filter_by(post_id=post.id).one().more_info_url == 'https://info.example/other'
        assert db.session.get(Post, post.id).url is None

        update_post_from_activity(post, {'object': dict(update, attachment=[])})
        assert Event.query.filter_by(post_id=post.id).one().more_info_url is None


# --------------------------------------------------------------------------
# The template, and the local form it now agrees with
# --------------------------------------------------------------------------


def test_the_template_links_the_online_link_with_no_guard_of_its_own():
    """Why the ingest is the boundary rather than the template.

    `post/_post_full.html` renders `event.online_link` straight into an `href` -- no
    `startswith('http')` in the way, unlike `user/show_profile.html`'s treatment of a
    user's extra field. That is the reason `_as_url` has to be exact; this row fails if
    a template gains a guard and the reader of this file should then be told the
    boundary moved.
    """
    from pathlib import Path

    template = (Path(__file__).resolve().parent.parent
                / 'app' / 'templates' / 'post' / '_post_full.html').read_text()

    assert '<a href="{{ event.online_link }}"' in template


def test_the_local_form_requires_the_same_scheme():
    """The two producers of an Event, asserted as a pair. `CreateEventForm` has always
    required `^https?://`; the federated path now does the same, so a link stored by
    either route means the same thing."""
    from app.community.forms import CreateEventForm

    validators = CreateEventForm.online_link.kwargs['validators']
    regexes = [v.regex.pattern for v in validators if hasattr(v, 'regex')]

    assert any('https?://' in pattern for pattern in regexes)
