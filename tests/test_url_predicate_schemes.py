"""D1409: the other two predicates that classified a url by its tail.

D1408 fixed `is_image_url`, which decided whether a string was an image by sniffing an
extension off `urlparse(url).path` -- and for a `javascript:` url the whole string after the
colon IS the path, so the attacker chooses the extension. Two more predicates in the same
file had the same shape:

    is_video_url('javascript:x/y.mp4')                 True
    is_video_hosting_site('javascript:videos/watch')   True

`is_video_url` is the literal twin: the same two lines, one function down.
`is_video_hosting_site` matches seven anchored `https://` prefixes and then, for PeerTube,
an UNBOUNDED substring -- `if 'videos/watch' in url` -- so any url carrying those eleven
characters anywhere qualified. `_post_full.html:195` and `post_teaser/_macros.html:403` gate
a PeerTube iframe on the same substring and feed it `Post.peertube_embed()`
(`self.url.replace('/videos/watch/', '/videos/embed/', 1)`), so the template shares the shape.

NEITHER IS A LIVE HOLE TODAY, AND BOTH ARE FIXED ANYWAY. No caller stores a url on these
answers -- they set `post.type` to POST_TYPE_VIDEO -- and `Post.url` can no longer hold one
of the blocked schemes (D1404 at federated ingest, D1407 from the API). They are fixed for
the reason `livescript` and `mocha` are in `UNSAFE_URL_SCHEMES`: the set is a blocklist and
they belong in it, and a predicate that answers a question about a url should not answer it
from a string the scheme makes meaningless.

THE RULE IS PINNED OVER THE SOURCE, not just over these two functions. An AST pass finds
every `endswith` test against a tuple or list of file extensions inside `app/`, and requires
each enclosing function to consult `has_unsafe_url_scheme`. Round 211 pinned `re.match`
against a `$`-anchored pattern the same way, for the same reason: the third site is the one
nobody reviews.
"""
import ast
import pathlib

import pytest

from app.utils import is_image_url, is_video_hosting_site, is_video_url
from tests.app_source import app_trees

APP = pathlib.Path(__file__).resolve().parent.parent / 'app'

UNSAFE = [
    'javascript:x/y.mp4',
    'JavaScript:x/y.mp4',
    ' javascript:x/y.mp4',
    'java\tscript:x/y.mp4',
    'vbscript:x/y.webm',
    'livescript:x/y.mp4',
    'mocha:x/y.mp4',
    'data:video/mp4;base64,AAAA/x.mp4',
]


@pytest.fixture(autouse=True)
def no_head(monkeypatch):
    """`is_image_url` reaches the network through `mime_type_using_head`; the two video
    predicates never do. Stubbed for the whole module so the one shared row that touches
    `is_image_url` cannot depend on a request."""
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')


class TestIsVideoUrl:
    @pytest.mark.parametrize('url', UNSAFE)
    def test_a_url_naming_an_unsafe_scheme_is_not_a_video(self, app, url):
        assert is_video_url(url) is False

    @pytest.mark.parametrize('url', ['https://example.test/clip.mp4',
                                     'http://example.test/clip.webm',
                                     'app/static/media/posts/ab/cd/clip.mp4',
                                     '/static/media/posts/ab/cd/clip.webm'])
    def test_a_real_video_url_is_still_a_video(self, app, url):
        """The control, and the reason this is a blocklist rather than an http(s)
        allowlist: the last two rows are the shape `process_upload` returns when S3 is
        off, and `app/shared/post.py` asks this predicate about them."""
        assert is_video_url(url) is True

    def test_a_url_with_no_video_extension_is_still_not_a_video(self, app):
        assert is_video_url('https://example.test/page.html') is False

    def test_the_bare_scheme_was_already_refused(self, app):
        """Why the shape hides: `javascript:alert(1)` has no video extension either, so
        the obvious probe answered correctly. The extension is free text."""
        assert is_video_url('javascript:alert(1)') is False


class TestIsVideoHostingSite:
    @pytest.mark.parametrize('url', ['javascript:videos/watch',
                                     'javascript:alert(1)/videos/watch/1',
                                     'data:text/html,videos/watch',
                                     'vbscript:videos/watch'])
    def test_an_unsafe_scheme_is_not_a_video_hosting_site(self, app, url):
        assert is_video_hosting_site(url) is False

    @pytest.mark.parametrize('url', ['https://peertube.example/videos/watch/abc',
                                     'http://peertube.example/videos/watch/abc',
                                     'https://youtube.com/watch?v=abc',
                                     'https://youtu.be/abc',
                                     'https://vimeo.com/1',
                                     'https://streamable.com/1',
                                     'https://www.redgifs.com/watch/x'])
    def test_the_real_hosts_still_match(self, app, url):
        """Both PeerTube rows matter: the substring test now requires an http(s) scheme,
        and `http://` is deliberately still accepted -- an instance served over plain
        HTTP is unusual, not impossible, and this predicate is not the place to refuse
        it."""
        assert is_video_hosting_site(url) is True

    def test_a_relative_url_carrying_the_substring_no_longer_matches(self, app):
        """The narrowing, stated. A scheme-relative or relative url is not a video hosting
        site; before this it was, because the substring test asked nothing about the rest
        of the string."""
        assert is_video_hosting_site('//evil.example/videos/watch/1') is False
        assert is_video_hosting_site('/videos/watch/1') is False

    def test_an_ordinary_url_is_still_not_one(self, app):
        assert is_video_hosting_site('https://example.test/article') is False


class TestFileIsImage:
    """`File.is_image` (app/models.py) is the third copy of the shape, and the sweep below
    is what found it: same extension list, same `urlparse(...).path`, over
    `thumbnail_url()` -- which falls back to `source_url`, a value a peer or an API client
    supplied. `admin/media.html:34` gates a link on this method:

        {% if file.is_image() %}<a href="{{ file.view_url() }}">...
    """

    def _file(self, source_url):
        from app.models import File

        return File(source_url=source_url)

    @pytest.mark.parametrize('url', ['javascript:alert(1)/x.png',
                                     'JavaScript:alert(1)/x.png',
                                     'data:image/svg+xml,<svg/>.png',
                                     'vbscript:x/y.png'])
    def test_a_file_whose_source_url_names_an_unsafe_scheme_is_not_an_image(self, app, url):
        assert self._file(url).is_image() is False

    @pytest.mark.parametrize('url', ['https://example.test/pic.png',
                                     'http://example.test/pic.jpeg'])
    def test_a_real_image_file_still_is_one(self, app, url):
        assert self._file(url).is_image() is True

    def test_a_locally_stored_file_is_unaffected(self, app):
        """`thumbnail_url()` returns a SERVER_URL-prefixed path when `thumbnail_path` is
        set, so the common case never reaches the scheme test with a peer's string at
        all -- asserted so the fix is known not to have touched it."""
        from app.models import File

        f = File(thumbnail_path='app/static/media/posts/ab/cd/x.png')

        assert f.is_image() is True


# --------------------------------------------------------------------------
# The rule, over the source
# --------------------------------------------------------------------------


def _extension_sniffing_functions():
    """Every function in `app/` that tests `endswith` against a collection of file
    extensions, with whether it also consults `has_unsafe_url_scheme`.

    Deliberately shaped like round 211's anchored-validator sweep: it reads the AST rather
    than grepping, so a predicate spelled across several lines is still found, and it
    prints every site rather than the first (fact 820).
    """
    found = []
    for path, _source, tree in app_trees():
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            # Every list/tuple literal in the function whose elements all look like file
            # extensions. `is_image_url` and `is_video_url` bind theirs to a name and then
            # iterate it inside a generator expression
            # (`any(path.endswith(ext) for ext in common_image_extensions)`), so the
            # `endswith` argument is a comprehension variable and not a literal -- which
            # is why this looks for the COLLECTION anywhere in the function rather than at
            # the call.
            extension_lists = [
                node for node in ast.walk(fn)
                if isinstance(node, (ast.Tuple, ast.List)) and node.elts
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                        and e.value.startswith('.') for e in node.elts)]
            sniffs = bool(extension_lists) and any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'endswith' for node in ast.walk(fn))
            if not sniffs:
                continue
            guards = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                         and n.func.id == 'has_unsafe_url_scheme'
                         for n in ast.walk(fn))
            found.append((path.relative_to(APP.parent).as_posix(), fn.lineno, fn.name,
                          guards))
    return found


def test_every_extension_sniffing_predicate_checks_the_scheme_first():
    """The sweep, as a test. A new predicate that classifies a url by its extension and
    forgets the scheme fails here rather than being found by the next review.

    Functions that sniff extensions for a reason other than classifying a URL -- an upload's
    filename, for instance -- are listed in the exemption below WITH their reason, so the
    exemption is an argument and not a hole.
    """
    exempt = {
        # Not classifying a URL. Each entry states what it IS sniffing, because an
        # exemption without a reason is just a hole with a comment.
        #
        # An uploaded FILE's own name, which has no scheme and never becomes an href:
        ('app/shared/upload.py', 'process_upload'),
        ('app/utils.py', 'guess_mime_type'),
        # `request.path` -- this instance's own request path, already split from the
        # scheme by the WSGI layer -- tested only to skip a CmsPage lookup for static
        # assets (app/errors/handlers.py:9).
        ('app/errors/handlers.py', 'not_found_error'),
        # An uploaded file's name again, in the two functions that decide which
        # extensions a post may carry (`allowed_extensions`, app/shared/post.py:245, 523).
        ('app/shared/post.py', 'make_post'),
        ('app/shared/post.py', 'edit_post'),
        # A `source_url` that is ALREADY STORED, asked only whether to skip resizing a
        # gif or to accept an avif's octet-stream Content-Type. Every producer of that
        # column now checks the scheme (D1405, D1407, D1408), and by this point the fetch
        # has already happened, so a check here would decide nothing.
        ('app/activitypub/util.py', 'make_image_sizes_async'),
    }

    unguarded = [row for row in _extension_sniffing_functions()
                 if not row[3] and (row[0], row[2]) not in exempt]

    assert unguarded == [], (
        'these classify a url by its extension without checking its scheme: '
        + '; '.join(f'{p}:{line} {name}' for p, line, name, _ in unguarded))


def test_the_sweep_finds_the_three_predicates_it_is_about():
    """The sweep's own control. An AST pass that matched nothing would make the row above
    pass for the wrong reason -- the failure round 199 recorded, where a guard that could
    never fire looked like a passing test."""
    names = {(p, name) for p, _, name, _ in _extension_sniffing_functions()}

    assert ('app/utils.py', 'is_image_url') in names
    assert ('app/utils.py', 'is_video_url') in names


def test_the_two_video_predicates_are_the_ones_this_round_changed():
    """Named here so the reader of this file can find the fix from the test."""
    source = (APP / 'utils.py').read_text()

    assert source.count('if has_unsafe_url_scheme(url):') >= 2
    assert "if url.lower().startswith(('http://', 'https://')) and 'videos/watch' in url:" \
        in source
