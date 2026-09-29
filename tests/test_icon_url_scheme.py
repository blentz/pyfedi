"""D1408: `is_image_url` said yes to `javascript:alert(1)/x.png`.

Six writes to `File.source_url` in `app/shared` are gated on one predicate:

    if icon_url and (from_scratch or icon_url_changed) and is_image_url(icon_url):
        file = File(source_url=icon_url)

and `is_image_url` decides by sniffing an extension off `urlparse(url).path`. For a
`javascript:` url the whole of the string after the colon IS the path, so an extension can
be put there. Measured:

    PROBE is_image_url('javascript:alert(1)')            False
    PROBE is_image_url('javascript:alert(1)/x.png')      True
    PROBE is_image_url('javascript:x//y.png')            True
    PROBE is_image_url('data:image/svg+xml,<svg/>.png')  True

The bare `javascript:alert(1)` was refused, which is why this looked guarded. In an href
the script evaluated from `javascript:alert(1)/x.png` is `alert(1)/x.png`: the alert runs
and the division is nonsense nobody sees. Measured end to end, through the API's community
edit:

    PROBE community icon source_url = 'javascript:alert(1)/x.png'
    PROBE community icon_image()    = 'javascript:alert(1)/x.png'
    PROBE community header_image()  = 'javascript:alert(1)/x.png'

`admin/edit_community.html:35` renders `header_image()` in a bare `href`, and both methods
return `source_url` unchanged through `served_path`, which rewrites only this instance's own
`app/` paths.

TWO FIXES, TWO DIFFERENT LISTS, AND THE REASON IS THE CALLERS.

* `is_image_url` refuses `UNSAFE_URL_SCHEMES` -- a BLOCKLIST -- because its callers
  legitimately pass values that are not http urls at all: `process_upload` returns
  `app/static/media/...` for a web upload whenever S3 is off, and an allowlist there would
  refuse every uploaded community icon, feed icon and banner. A path with no scheme has no
  scheme to block, so it still passes. `data:` is in the set even though `<img
  src="data:image/png;...">` is harmless, because what the predicate gates is STORING the
  string, and `make_image_sizes` cannot fetch a data: url -- it was never a working image.
* the API's `icon_url` and `banner_url` get the http(s) ALLOWLIST at their own boundary, in
  `edit_community`, `make_feed` and `edit_feed`, because a url an API client supplies is one
  this instance fetches. Refused rather than dropped, like the API's other urls (D1405,
  D1407).

FACT 904 FOR THE THIRD TIME: eight more `fields.String(metadata={"format": "url"})`
declarations -- `CreateCommunityRequest`, `EditCommunityRequest`, `CreateFeedRequest` and
`EditFeedRequest`, icon and banner each -- documenting a format that marshmallow never
checks.
"""
import pytest

from app import db
from app.constants import SRC_API
from app.models import File
from app.utils import is_image_url

HOSTILE = 'javascript:alert(1)/x.png'
PLAIN = 'javascript:alert(1)'

# Every value the old predicate accepted, plus the shapes a fix narrowed to the literal
# string 'javascript:' would let through.
UNSAFE_IMAGE_URLS = [
    HOSTILE,
    'javascript:x//y.png',
    'JavaScript:alert(1)/x.png',
    ' javascript:alert(1)/x.png',
    'java\tscript:alert(1)/x.png',
    'vbscript:msgbox(1)/x.png',
    'livescript:alert(1)/x.png',
    'mocha:alert(1)/x.png',
    'data:image/svg+xml,<svg/>.png',
]

# Values `is_image_url` must keep accepting. The first two are what a web upload produces
# when S3 is off, and they are why this predicate is a blocklist.
STILL_IMAGE_URLS = [
    'app/static/media/communities/ab/cd/abcdef.png',
    '/static/media/posts/ab/cd/abcdef.jpg',
    'https://example.test/pic.png',
    'http://example.test/pic.jpeg',
    'https://example.test/pic.svg+xml',
]


# --------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------


class TestIsImageUrl:
    @pytest.fixture(autouse=True)
    def no_head(self, monkeypatch):
        """`is_image_url` asks `mime_type_using_head` before sniffing the extension, and
        '' is the "no Content-Type could be determined" answer that function already
        returns for an HTTPError -- so every row below turns on the extension logic and
        not on the network."""
        monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')

    @pytest.mark.parametrize('url', UNSAFE_IMAGE_URLS)
    def test_a_url_naming_an_unsafe_scheme_is_not_an_image(self, app, url):
        assert is_image_url(url) is False

    @pytest.mark.parametrize('url', STILL_IMAGE_URLS)
    def test_the_values_the_callers_really_pass_are_still_images(self, app, url):
        """The control, and the reason this is a blocklist. The first two rows are what
        `process_upload` returns when S3 is off; an http(s) allowlist here would refuse
        every uploaded icon and banner."""
        assert is_image_url(url) is True

    def test_a_bare_javascript_url_was_already_refused(self, app):
        """Why the hole was invisible: the obvious probe answered correctly, because
        `alert(1)` has no image extension. The extension is the attacker's to choose."""
        assert is_image_url(PLAIN) is False

    def test_a_url_with_no_image_extension_is_still_not_an_image(self, app):
        assert is_image_url('https://example.test/page.html') is False

    def test_a_content_type_can_still_decide(self, app, monkeypatch):
        """The HEAD branch above the extension sniff is untouched by the scheme check --
        but only for a url whose scheme survived it."""
        monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: 'image/png')

        assert is_image_url('https://example.test/no-extension') is True
        assert is_image_url('javascript:alert(1)/no-extension') is False


# --------------------------------------------------------------------------
# A community's icon and banner, through the API
# --------------------------------------------------------------------------


@pytest.fixture
def community_api(app, db_session, monkeypatch):
    from types import SimpleNamespace

    from app.shared import community as community_module
    from tests.test_shared_community_lifecycle import _seed

    s = _seed()
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')
    monkeypatch.setattr(community_module, 'make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr(community_module, 'task_selector', lambda *a, **k: None)
    monkeypatch.setattr(community_module, 'authorise_api_user', lambda *a, **k: s.user)
    return SimpleNamespace(module=community_module, user=s.user, community=s.community)


def _edit_community(community_api, **over):
    from tests.test_shared_community_lifecycle import _api_input

    return community_api.module.edit_community(
        _api_input(**over), community_api.community, SRC_API, auth='token',
        from_scratch=True)


class TestACommunityIcon:
    @pytest.mark.parametrize('url', [HOSTILE, 'data:image/svg+xml,<svg/>.png',
                                     '/relative.png', 'example.test/pic.png'])
    def test_an_icon_url_that_is_not_http_is_refused(self, community_api, url):
        with pytest.raises(Exception, match='icon_url must be an http:// or https:// url'):
            _edit_community(community_api, icon_url=url)

        assert community_api.community.icon_id is None

    def test_a_banner_url_that_is_not_http_is_refused(self, community_api):
        """Named separately in the message, because a client that sent one bad value and
        one good one should be told which."""
        with pytest.raises(Exception,
                           match='banner_url must be an http:// or https:// url'):
            _edit_community(community_api, icon_url='https://ok.example/i.png',
                            banner_url=HOSTILE)

        assert community_api.community.image_id is None

    def test_real_urls_are_accepted_and_stored(self, community_api):
        """The control. Both files are created, which also proves this fixture reaches the
        image-persistence block rather than stopping earlier."""
        _edit_community(community_api, icon_url='https://ok.example/i.png',
                        banner_url='https://ok.example/b.png')
        db.session.commit()

        assert db.session.get(File, community_api.community.icon_id).source_url == \
            'https://ok.example/i.png'
        assert db.session.get(File, community_api.community.image_id).source_url == \
            'https://ok.example/b.png'

    def test_no_icon_at_all_is_not_a_refusal(self, community_api):
        """`if value and ...` -- an edit that says nothing about the icon must still be
        allowed, and `_api_input` defaults both to None."""
        _edit_community(community_api)

        assert community_api.community.icon_id is None

    def test_the_rendered_value_is_what_the_template_gets(self, community_api):
        """The sink, stated once. `icon_image()` and `header_image()` return `source_url`
        unchanged through `served_path`, and `admin/edit_community.html:35` puts
        `header_image()` in a bare href -- so what is refused above is exactly what would
        have been rendered."""
        _edit_community(community_api, icon_url='https://ok.example/i.png',
                        banner_url='https://ok.example/b.png')
        db.session.commit()

        assert community_api.community.icon_image() == 'https://ok.example/i.png'
        assert community_api.community.header_image() == 'https://ok.example/b.png'


# --------------------------------------------------------------------------
# A feed's icon and banner, at both of its boundaries
# --------------------------------------------------------------------------


@pytest.fixture
def feed_api(app, db_session, monkeypatch):
    from types import SimpleNamespace

    from app.shared import feed as feed_module
    from tests.test_shared_feed_edit import _seed

    s = _seed()
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')
    monkeypatch.setattr(feed_module, 'make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr(feed_module, 'task_selector', lambda *a, **k: None)
    monkeypatch.setattr(feed_module, 'authorise_api_user', lambda *a, **k: s.owner)
    return SimpleNamespace(module=feed_module, owner=s.owner, feed=s.feed)


class TestAFeedIcon:
    @pytest.mark.parametrize('field', ['icon_url', 'banner_url'])
    def test_an_edit_refuses_a_url_that_is_not_http(self, feed_api, field):
        from tests.test_shared_feed_edit import _api_payload

        with pytest.raises(Exception, match=f'{field} must be an http:// or https:// url'):
            feed_api.module.edit_feed(_api_payload(**{field: HOSTILE}), feed_api.feed,
                                      SRC_API, auth='token', from_scratch=True)

    def test_an_edit_still_accepts_a_real_url(self, app, feed_api):
        """The control for the edit boundary.

        `_site_ctx` is needed only here: `edit_feed` reads `g.site.enable_nsfw` BELOW the
        check, and `before_request` -- which populates `g.site` in the real app -- does not
        run in a test context. The refusal rows above never get that far, which is itself
        a small witness that the guard sits above the rest of the function."""
        from tests.test_shared_feed_edit import _api_payload, _site_ctx

        with _site_ctx(app, feed_api.owner):
            feed_api.module.edit_feed(_api_payload(icon_url='https://ok.example/i.png'),
                                      feed_api.feed, SRC_API, auth='token',
                                      from_scratch=True)
        db.session.commit()

        assert db.session.get(File, feed_api.feed.icon_id).source_url == \
            'https://ok.example/i.png'

    @pytest.mark.parametrize('field', ['icon_url', 'banner_url'])
    def test_creation_refuses_one_too(self, feed_api, field):
        """`make_feed` has its own copy of the SRC_API field extraction -- the same shape
        `edit_feed` has -- so it needs its own check. A fix applied to one and not the
        other is this campaign's commonest shape."""
        from tests.test_shared_feed_edit import _api_payload

        payload = _api_payload(url='brandnewfeed', title='Brand new', **{field: HOSTILE})

        with pytest.raises(Exception, match=f'{field} must be an http:// or https:// url'):
            feed_api.module.make_feed(payload, SRC_API, auth='token')


# --------------------------------------------------------------------------
# The schemas
# --------------------------------------------------------------------------


def test_eight_more_schema_fields_still_only_document_the_format():
    """Fact 904's third appearance. `metadata` is OpenAPI documentation; marshmallow does
    not read it. If a real `validate=` is added later this row fails, and the handler
    checks can be reconsidered rather than silently duplicated."""
    from app.api.alpha.schema import (CreateCommunityRequest, CreateFeedRequest,
                                      EditCommunityRequest, EditFeedRequest)

    for schema in [CreateCommunityRequest, EditCommunityRequest, CreateFeedRequest,
                   EditFeedRequest]:
        for field in ['icon_url', 'banner_url']:
            declared = schema().fields[field]
            assert declared.metadata.get('format') == 'url', (schema, field)
            assert declared.validators == [], (schema, field)
