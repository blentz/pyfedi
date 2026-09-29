"""D1407: the API half of the two link holes the federated path already had.

Four rounds fixed the peer side of three fields. This is the LOCAL producer of two of
them, and it was open to any authenticated API client, because the web forms validate and
the API schema only documents:

* `CreatePostRequest.url` and `EditPostRequest.url` are
  `fields.String(metadata={"format": "url"})` (`app/api/alpha/schema.py:1386, 1398`).
  `app/shared/post.py` took the value verbatim for `SRC_API`, and the only check below it
  is `domain_from_url(url)` for a banned domain -- which answers None for
  `javascript:alert(1)` and skips the whole block. The web path takes the same column from
  `CreateLinkForm.link_url` / `CreateVideoForm.video_url`, both `Regexp(r'^https?://')`.
  Sink: the ten bare `href="{{ post.url }}"` sites of D1404.
* `CreatePostRequest.event` / `EditPostRequest.event` nest `PostEvent`
  (`app/api/alpha/schema.py:351-363`), whose `online_link`,
  `external_participation_url` and `buy_tickets_link` are the same declaration. The
  event-write block in `app/shared/post.py` wrote all three straight onto the row. The web
  form supplies only `online_link`, already scheme-checked. Sink:
  `post/_post_full.html:219`, the D1403 href.

Measured, through `make_post(..., src=SRC_API)`:

    PROBE api post.url                   = 'javascript:alert(document.domain)'
    PROBE api online_link                = 'javascript:alert(document.domain)'
    PROBE api external_participation_url = 'javascript:alert(2)'
    PROBE api buy_tickets_link           = 'javascript:alert(3)'

So fixing the federated path in D1403 and D1404 closed the harder route and left the
easier one open: a peer needs its own instance, an API client needs an account.

TWO DIFFERENT ANSWERS, ON PURPOSE. The post `url` is REFUSED, with the exception the API
already uses for bad input and the same treatment D1405 gave `avatar`/`cover`: the caller
is waiting and can fix the value, and a link post whose url was silently dropped is not
the post they asked for. The three event links are DROPPED, matching `_as_url`'s use on
the federated side: None is what those columns hold for an event that named no link, and
the rest of the event is still what the author asked for.

WHERE THE EVENT RULE GOES. One block in `app/shared/post.py` writes those three columns
for BOTH the web form and the API, so the check there covers both producers -- the fourth
and fifth of five, after `Post.new`, `update_post_from_activity` and the form's own
`Regexp`.
"""
import pytest
from flask import g

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_LINK, SRC_API
from app.models import Event, Post
from tests.test_shared_post_edit import _api_input
from tests.test_shared_post_make import seed_make_context

HOSTILE = 'javascript:alert(document.domain)'

NOT_HTTP = [
    HOSTILE,
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    '//evil.example/x',
    '/relative',
    'example.test/no-scheme',
    "javascript:fetch('https://evil.example/steal')",
]


@pytest.fixture
def api(db_session, monkeypatch):
    """`seed_make_context`'s keyed local author, with the outbound work stubbed.

    `with_keys=True` is load-bearing there (`can_create_post` refuses a local user whose
    `private_key` is None), and `task_selector`/`make_image_sizes` are stubbed because
    every accepted row would otherwise queue federation and fetch an image.
    """
    from types import SimpleNamespace
    s = seed_make_context('apiposts')
    monkeypatch.setattr('app.shared.post.task_selector', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')
    monkeypatch.setattr('app.shared.post.authorise_api_user', lambda *a, **k: s.author)
    return SimpleNamespace(author=s.author, community=s.community, site=s.site)


def _make(api, type_, **over):
    from app.shared.post import make_post

    result = make_post(_api_input(**over), api.community, type_, SRC_API, auth='token')
    # `make_post` returns `(user.id, post)` for SRC_API and a bare post for the web.
    return result[1] if isinstance(result, tuple) else result


def _event_input(**over):
    event = {'start': '2050-01-01T00:00:00Z', 'end': '2050-01-01T01:00:00Z',
             'timezone': 'UTC', 'online': True}
    event.update(over)
    return {'event': event}


# --------------------------------------------------------------------------
# The post url: refused
# --------------------------------------------------------------------------


class TestThePostUrl:
    @pytest.mark.parametrize('url', NOT_HTTP)
    def test_a_url_that_is_not_http_is_refused(self, api, url):
        with pytest.raises(Exception, match='http:// or https://'):
            _make(api, POST_TYPE_LINK, url=url)

        assert Post.query.count() == 0

    def test_the_refusal_happens_before_any_counter_moves(self, api):
        """WHY `make_post` NEEDS ITS OWN COPY OF THE GUARD, and the row that proves it.

        `make_post` inserts the Post, increments `community.post_count` and
        `user.post_count`, writes the author's self-vote, and only then hands off to
        `edit_post` (`app/shared/post.py:277`, `from_scratch=True`), whose SRC_API branch
        re-reads the same `input['url']` and applies the same check. So with `make_post`'s
        copy removed the request is still refused -- by `edit_post` -- and every row
        asserting a raise, and even `Post.query.count() == 0`, keeps passing. Measured with
        that copy replaced by `if False:`:

            PROBE raised: Exception('url must be an http:// or https:// url')
            PROBE posts:  []
            PROBE votes:  0  post_count: 1

        The insert is undone and the COUNTER IS NOT: a refused API call left the community
        claiming one more post than it has, which is the only observable difference
        between checking early and checking late. Two mutation survivors said so before
        this row existed.
        """
        before_community = api.community.post_count
        before_user = api.author.post_count

        with pytest.raises(Exception, match='http:// or https://'):
            _make(api, POST_TYPE_LINK, url=HOSTILE)

        assert api.community.post_count == before_community
        assert api.author.post_count == before_user

    @pytest.mark.parametrize('url', NOT_HTTP)
    def test_an_edit_cannot_set_one_either(self, api, url):
        """The second boundary. A post created with a good url must not be editable into a
        bad one -- the same asymmetry D1403's Update rows cover on the federated side."""
        from app.shared.post import edit_post

        post = _make(api, POST_TYPE_LINK, url='https://ok.example/x')
        db.session.commit()

        with pytest.raises(Exception, match='http:// or https://'):
            edit_post(_api_input(url=url), post, POST_TYPE_LINK, SRC_API,
                      user=api.author)

        assert db.session.get(Post, post.id).url == 'https://ok.example/x'

    def test_a_real_url_is_accepted(self, api):
        """The control. A guard that refused everything would pass every row above and
        break posting links through the API entirely."""
        post = _make(api, POST_TYPE_LINK, url='https://ok.example/article')

        assert post.url == 'https://ok.example/article'

    def test_a_post_with_no_url_is_untouched(self, api):
        """`if url and ...` -- an article has no url, and the guard must not turn that
        into a refusal."""
        post = _make(api, POST_TYPE_ARTICLE, url=None)

        assert post is not None
        assert post.url is None

    def test_a_hostless_http_url_still_reaches_the_domain_check(self, api):
        """The guard is about the SCHEME and nothing else. `https:///x` parses, has no
        hostname, and is exactly the shape two rows in tests/test_shared_post_make.py and
        tests/test_shared_post_edit.py use to reach `domain_from_url`'s None arm -- both
        had to move off `file:` and `not-a-url` when this guard landed, and this row says
        why that shape still works."""
        post = _make(api, POST_TYPE_LINK, url='https:///x')

        assert post is not None
        assert post.url is not None


# --------------------------------------------------------------------------
# The three event links: dropped
# --------------------------------------------------------------------------


class TestTheEventLinks:
    @pytest.mark.parametrize('url', NOT_HTTP)
    def test_none_of_the_three_keeps_a_value_that_is_not_http(self, api, url):
        post = _make(api, POST_TYPE_EVENT,
                     **_event_input(online_link=url, external_participation_url=url,
                                    buy_tickets_link=url))
        db.session.commit()
        event = Event.query.filter_by(post_id=post.id).one()

        assert event.online_link is None
        assert event.external_participation_url is None
        assert event.buy_tickets_link is None

    def test_a_real_link_is_kept(self, api):
        post = _make(api, POST_TYPE_EVENT,
                     **_event_input(online_link='https://meet.example/room',
                                    external_participation_url='https://t.example/1',
                                    buy_tickets_link='http://shop.example/1'))
        db.session.commit()
        event = Event.query.filter_by(post_id=post.id).one()

        assert event.online_link == 'https://meet.example/room'
        assert event.external_participation_url == 'https://t.example/1'
        assert event.buy_tickets_link == 'http://shop.example/1'

    def test_the_rest_of_the_event_still_arrives(self, api):
        """Dropped, not refused: the event is still worth creating, and the author gets
        the event they asked for minus a link a browser would not have fetched."""
        post = _make(api, POST_TYPE_EVENT,
                     **_event_input(online_link=HOSTILE, timezone='Europe/London'))
        db.session.commit()
        event = Event.query.filter_by(post_id=post.id).one()

        assert event.online_link is None
        assert event.timezone == 'Europe/London'
        assert event.online is True
        assert db.session.get(Post, post.id).type == POST_TYPE_EVENT

    def test_an_edit_cannot_set_one_either(self, api):
        """The same block serves `edit_post`, so this is the same line -- asserted because
        an event whose good link can be replaced by a bad one is the failure that matters,
        not the creation path alone."""
        from app.shared.post import edit_post

        post = _make(api, POST_TYPE_EVENT,
                     **_event_input(online_link='https://meet.example/room'))
        db.session.commit()

        edit_post(_api_input(**_event_input(online_link=HOSTILE)), post,
                  POST_TYPE_EVENT, SRC_API, user=api.author)
        db.session.commit()

        assert Event.query.filter_by(post_id=post.id).one().online_link is None

    def test_an_edit_can_still_change_a_link(self, api):
        from app.shared.post import edit_post

        post = _make(api, POST_TYPE_EVENT,
                     **_event_input(online_link='https://meet.example/room'))
        db.session.commit()

        edit_post(_api_input(**_event_input(online_link='https://meet.example/other')),
                  post, POST_TYPE_EVENT, SRC_API, user=api.author)
        db.session.commit()

        assert Event.query.filter_by(post_id=post.id).one().online_link == \
            'https://meet.example/other'


# --------------------------------------------------------------------------
# The schema, which is where the reader will look first
# --------------------------------------------------------------------------


def test_the_schema_still_only_documents_the_format():
    """Fact 904, pinned where it applies. `metadata` is OpenAPI documentation and
    marshmallow does not validate it, so these declarations do not constrain the value --
    and if someone later adds a real `validate=`, this row fails and the handler checks
    can be reconsidered rather than silently duplicated."""
    from app.api.alpha.schema import CreatePostRequest, EditPostRequest, PostEvent

    for schema, field in [(CreatePostRequest, 'url'), (EditPostRequest, 'url'),
                          (PostEvent, 'online_link'),
                          (PostEvent, 'external_participation_url'),
                          (PostEvent, 'buy_tickets_link')]:
        declared = schema().fields[field]
        assert declared.metadata.get('format') == 'url', (schema, field)
        assert declared.validators == [], (schema, field)


def test_the_web_form_requires_what_the_api_now_requires():
    """The twin that already agreed, for the fields it offers. `CreateLinkForm.link_url`
    and `CreateVideoForm.video_url` carry the same rule the API handler now applies."""
    from app.community.forms import CreateLinkForm, CreateVideoForm

    for field in [CreateLinkForm.link_url, CreateVideoForm.video_url]:
        patterns = [v.regex.pattern for v in field.kwargs['validators']
                    if hasattr(v, 'regex')]
        assert any('https?://' in pattern for pattern in patterns), field
