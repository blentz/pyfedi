"""D1404: a peer's attachment url reached `Post.url`, and `Post.url` is an href.

`Post.url` is rendered as a bare `href` in ten places -- `post/_post_full.html:8`, five
sites in `post/post_teaser/_macros.html`, four in `post/_post_teaser_masonry.html`, two of
which also make it an `img src` -- and `post_to_page` (`app/activitypub/util.py:174`)
federates it back out again as a Link attachment. Between a peer's attachment and all of
that stood one guard, `url_is_parseable`, whose own docstring says what it does not do:

    Deliberately narrow: it answers only "does urlparse accept this", not "is
    this a good URL". No scheme check, no host check, no length check

`urlparse('javascript:alert(1)')` does not raise, so it passed. Measured, for every
attachment shape this codebase reads and for the Update path:

    PROBE Post.new   Link/href (Lemmy < 0.19.4)  post.url='javascript:alert(document.domain)' type=1
    PROBE Post.new   Link/url (NodeBB)           post.url='javascript:alert(document.domain)' type=1
    PROBE Post.new   Document/url (Mastodon)     post.url='javascript:alert(document.domain)' type=1
    PROBE Post.new   Audio/url (WordPress)       post.url='javascript:alert(document.domain)' type=1
    PROBE Post.new   Image/url (PixelFed)        post.url='javascript:alert(document.domain)' type=1
    PROBE Post.new   dict (a.gup.pe)             post.url='javascript:alert(document.domain)' type=1
    PROBE update     before='https://ok.example/x' after='javascript:alert(document.domain)'

`type=1` is POST_TYPE_LINK, which is the type whose templates render the url most
prominently -- a link post's whole body is the link. The same round fixed an Event's three
link fields (D1403); this is the same defect at the field every link post has.

WHY THIS ONE IS A BLOCKLIST WHEN D1403's WAS AN ALLOWLIST. `_as_url` allowlists `http(s)`
for an Event's links because `CreateEventForm` already required exactly that of the same
fields. All four local producers of `Post.url` require it too, so an allowlist is
defensible here as well and would be stronger. It was not taken, for the reason recorded at
`UNSAFE_URL_SCHEMES` (`app/utils.py:404`): this url is chosen by REMOTE software, and
allowlisting means auditing every scheme that legitimately appears in a link post across
the fediverse -- `magnet:`, `matrix:`, `xmpp:`, `gemini:`, `ipfs:` and a long tail -- where
being wrong silently drops real links from every remote instance. `url_is_storable` refuses
the schemes an href may never carry and keeps the rest, which closes the hole with no such
risk. The rows below pin BOTH halves of that decision, so an upgrade to an allowlist has to
change this file deliberately rather than by accident.
"""
import contextlib

import pytest
from flask import g

from app import db
from app.activitypub.util import post_to_page, update_post_from_activity
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_LINK
from app.models import Post, Site
from app.utils import url_is_storable
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'
HOSTILE = 'javascript:alert(document.domain)'

# Every scheme in UNSAFE_URL_SCHEMES, in the spellings the normalisation exists to catch.
UNSAFE = [
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    'vbscript:msgbox(1)',
    'livescript:alert(1)',
    'mocha:alert(1)',
    'data:text/html,<script>alert(1)</script>',
]

# Schemes the blocklist deliberately keeps. Each of these is a link a remote instance
# genuinely emits, and the comment at app/utils.py:404 is the reason an allowlist was not
# used here -- these rows are what would break if one were added without an audit.
STORABLE = [
    'https://example.test/article',
    'http://example.test/article',
    'magnet:?xt=urn:btih:0123456789abcdef',
    'matrix:r/room:example.test',
    'xmpp:someone@example.test',
    'gemini://example.test/page',
    'ipfs://bafyexample',
    'mailto:someone@example.test',
    'tel:+15550100',
    'ftp://example.test/file',
]


@pytest.fixture(autouse=True)
def no_head(monkeypatch):
    """`is_image_url` issues a real `httpx_client.head` through `mime_type_using_head`
    (app/utils.py:270, 333) for any url that reaches it. '' is the "no Content-Type could
    be determined" answer that function already returns for an HTTPError, and it sends
    `is_image_url` to extension sniffing -- so no row here depends on the network, and the
    unfixed code's HEAD request to `javascript:alert(...)` (which respx reported as
    `HEAD /alert(document.domain)`) does not decide anything either."""
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')


@pytest.fixture
def env(app, db_session):
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    local = make_instance(HOST, software='piefed')
    make_user(local, 'founder', local=True)
    peer = make_instance(PEER)
    author = make_user(peer, 'author')
    author.ap_profile_id = f'https://{PEER}/u/author'
    community = make_community('general')
    db.session.commit()
    return SimpleNamespace(author=author, community=community)


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    class _Lock:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr('app.redis_client', _Lock())


def _created(env, attachment, slug='1'):
    activity = {'id': f'https://{PEER}/activities/{slug}', 'type': 'Create',
                'actor': env.author.ap_profile_id,
                'object': {'id': f'https://{PEER}/p/{slug}', 'type': 'Page',
                           'name': 'A link post', 'attributedTo': env.author.ap_profile_id,
                           'audience': env.community.ap_profile_id,
                           'attachment': attachment}}
    post = Post.new(env.author, env.community, activity)
    db.session.commit()
    return post


# Every attachment shape `Post.new` reads, each named for the software that sends it.
# A fix applied to one branch and not the others is the failure this table exists for.
SHAPES = {
    'Link/href, Lemmy < 0.19.4': lambda url: [{'type': 'Link', 'href': url}],
    'Link/url, NodeBB': lambda url: [{'type': 'Link', 'url': url}],
    'Document/url, Mastodon': lambda url: [{'type': 'Document', 'url': url}],
    'Audio/url, WordPress': lambda url: [{'type': 'Audio', 'url': url}],
    'Image/url, PixelFed and Lemmy >= 0.19.4': lambda url: [{'type': 'Image', 'url': url}],
    'a dict rather than a list, a.gup.pe': lambda url: {'url': url},
}


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


class TestTheRule:
    @pytest.mark.parametrize('url', UNSAFE)
    def test_a_scheme_an_href_may_not_carry_is_not_storable(self, url):
        assert url_is_storable(url) is False

    @pytest.mark.parametrize('url', STORABLE)
    def test_every_other_scheme_is_storable(self, url):
        """The deliberate half of a blocklist. `magnet:` and the rest are links remote
        software really sends; an allowlist of http(s) would drop all of them, which is
        why this is a blocklist and why that choice is pinned rather than assumed."""
        assert url_is_storable(url) is True

    def test_a_url_urlparse_refuses_is_still_not_storable(self):
        """The half that was already there. `url_is_storable` adds a scheme test to
        `url_is_parseable` and must not lose what it wrapped."""
        assert url_is_storable('https://[::1/x') is False

    @pytest.mark.parametrize('value', [None, 42, ['https://example.test'], b'https://x'])
    def test_a_value_that_is_not_a_string_is_not_storable(self, value):
        """Order matters: `url_scheme` calls `.strip()` on its argument, so a non-string
        reaching the scheme test is an AttributeError. Parseability is tested first and
        answers False for a non-string, which short-circuits it."""
        assert url_is_storable(value) is False

    def test_the_scheme_is_matched_whole_and_not_as_a_prefix(self):
        """Inherited from `has_unsafe_url_scheme`, and asserted here because a rewrite
        that compared prefixes would break real links."""
        assert url_is_storable('javascriptic:x') is True
        assert url_is_storable('https://example.test/javascript:alert(1)') is True


# --------------------------------------------------------------------------
# Post.new
# --------------------------------------------------------------------------


class TestIngestingANewPost:
    @pytest.mark.parametrize('shape', list(SHAPES))
    def test_no_attachment_shape_stores_an_unsafe_url(self, env, shape):
        post = _created(env, SHAPES[shape](HOSTILE), slug=shape[:8])

        assert post.url is None

    @pytest.mark.parametrize('shape', list(SHAPES))
    def test_every_attachment_shape_still_stores_a_real_url(self, env, shape):
        """The control, per shape. A fix that dropped every url would pass the table
        above and break link posts from all six kinds of peer."""
        post = _created(env, SHAPES[shape]('https://example.test/article'),
                        slug='ok' + shape[:6])

        assert post.url == 'https://example.test/article'

    def test_the_post_still_arrives_as_a_discussion(self, env):
        """A dropped url is not a dropped post: refusing the whole thing would hand
        peers a way to make this instance discard content. POST_TYPE_ARTICLE is what a
        post with no url is, so the type follows the data rather than being left at
        POST_TYPE_LINK with nothing to link to."""
        post = _created(env, SHAPES['Link/href, Lemmy < 0.19.4'](HOSTILE), slug='kept')

        assert post is not None
        assert post.title == 'A link post'
        assert post.url is None
        assert post.type == POST_TYPE_ARTICLE

    def test_a_real_link_post_is_still_typed_as_one(self, env):
        post = _created(env, SHAPES['Link/href, Lemmy < 0.19.4']('https://example.test/a'),
                        slug='link')

        assert post.type == POST_TYPE_LINK

    def test_an_events_link_attachment_uses_the_same_rule(self, env):
        """`Post.new`'s Event branch has its OWN url write and its own guard, below the
        Page path's -- the comment there records that a Mobilizon event's link once had
        no check at all. The two must not diverge on which schemes they accept."""
        activity = {'id': f'https://{PEER}/activities/ev', 'type': 'Create',
                    'actor': env.author.ap_profile_id,
                    'object': {'id': f'https://{PEER}/events/ev', 'type': 'Event',
                               'name': 'An event',
                               'attributedTo': env.author.ap_profile_id,
                               'audience': env.community.ap_profile_id,
                               'startTime': '2050-01-01T00:00:00Z',
                               'attachment': [{'type': 'Link', 'href': HOSTILE}]}}
        post = Post.new(env.author, env.community, activity)
        db.session.commit()

        assert post.url is None


# --------------------------------------------------------------------------
# update_post_from_activity
# --------------------------------------------------------------------------


class TestUpdatingAPost:
    @pytest.fixture
    def existing(self, env, redis_lock_only_double):
        """A post that already holds a good url, so an Update carrying a hostile one has
        something to overwrite. That is the failure worth catching: the probe measured a
        stored `https://ok.example/x` being replaced."""
        return _created(env, SHAPES['Link/href, Lemmy < 0.19.4']('https://ok.example/x'),
                        slug='upd')

    def test_an_update_cannot_store_an_unsafe_url(self, env, existing):
        update_post_from_activity(existing, {'object': {
            'id': existing.ap_id, 'type': 'Page', 'name': 'A link post',
            'attachment': [{'type': 'Link', 'href': HOSTILE}]}})
        db.session.commit()

        assert existing.url is None

    def test_an_unsafe_url_clears_the_link_rather_than_refusing_the_update(self, env,
                                                                          existing):
        """Recorded because it is a real cost of this design, not an accident. The guard
        sets `new_url` to the same None an unparseable url gets, so the url-change arm
        below it runs and the post becomes a discussion -- a peer can wipe a link by
        sending an Update naming a scheme this instance will not store. That is the
        behaviour already chosen for unparseable urls, and the comment at that guard
        gives the reason: rejecting the Update would let peers make us drop content.
        Storing the hostile url is not an option, so the alternative to this is keeping
        the OLD url, which is a different change with its own argument."""
        update_post_from_activity(existing, {'object': {
            'id': existing.ap_id, 'type': 'Page', 'name': 'A link post',
            'attachment': [{'type': 'Link', 'href': HOSTILE}]}})
        db.session.commit()

        assert existing.type == POST_TYPE_ARTICLE

    def test_an_update_can_still_change_a_url(self, env, existing):
        update_post_from_activity(existing, {'object': {
            'id': existing.ap_id, 'type': 'Page', 'name': 'A link post',
            'attachment': [{'type': 'Link', 'href': 'https://ok.example/other'}]}})
        db.session.commit()

        assert existing.url == 'https://ok.example/other'


# --------------------------------------------------------------------------
# The sinks: what made this a defect rather than untidy storage
# --------------------------------------------------------------------------


def test_the_templates_still_link_post_url_with_no_guard_of_their_own():
    """Why the ingest is the boundary. Ten bare `href="{{ post.url }}"` sites across three
    templates, none of them scheme-checked -- if one gains a guard, this row fails and
    whoever reads it should be told the boundary moved."""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / 'app' / 'templates' / 'post'
    found = sum(t.read_text().count('href="{{ post.url }}"')
                for t in [templates / '_post_full.html',
                          templates / 'post_teaser' / '_macros.html',
                          templates / '_post_teaser_masonry.html'])

    assert found >= 9


def test_a_dropped_url_is_not_federated_back_out(env):
    """`post_to_page` gates its outbound Link attachment on `post.url is not None`, so
    None means this instance does not relay a url it refused to store -- it would
    otherwise hand the hostile href to every peer that follows the community, which is
    what made storing it more than a local problem.

    The assertion is on the attachment's CONTENTS, not its presence: the key is
    initialised to `[]` earlier in `post_to_page` and other branches (an image post, a
    poll) fill it, so `'attachment' not in ...` would pass or fail for reasons that have
    nothing to do with the url. `''` rather than None would have federated
    `{"href": ""}`, which is why the guards write None."""
    post = _created(env, SHAPES['Link/href, Lemmy < 0.19.4'](HOSTILE), slug='fed')

    page = post_to_page(post)

    assert page['attachment'] == []
    assert HOSTILE not in repr(page)


def test_a_kept_url_still_federates_as_a_link_attachment(env):
    """The control for the row above: the attachment is empty because the url was
    dropped, not because this instance stopped sending link attachments."""
    post = _created(env, SHAPES['Link/href, Lemmy < 0.19.4']('https://ok.example/x'),
                    slug='fedok')

    assert post_to_page(post)['attachment'] == [{'href': 'https://ok.example/x',
                                                 'type': 'Link'}]


def test_every_local_producer_of_post_url_requires_an_http_scheme():
    """The twin. A local post's url cannot be `javascript:` -- four form fields say so --
    which is what made the federated path's silence a divergence rather than a policy."""
    from app.community.forms import CreateEventForm, CreateLinkForm, CreateVideoForm

    fields = [CreateLinkForm.link_url, CreateVideoForm.video_url,
              CreateEventForm.online_link, CreateEventForm.more_info_url]
    for field in fields:
        patterns = [v.regex.pattern for v in field.kwargs['validators']
                    if hasattr(v, 'regex')]
        assert any('https?://' in pattern for pattern in patterns), field
