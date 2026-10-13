"""A peer's url must not reach `Post.url` when a browser would visit a different host
from the one the domain-ban check reads.

`Post.new` bans a link post by `domain_from_url(post.url)`, which reads
`urlparse(url.lower()).hostname`. A browser reads the host by the WHATWG URL rules
instead, and for the special schemes (http, https, ws, wss, ftp) the two disagree:

    url                                       ban check reads     browser visits
    https:///banned.example                   (no host)           banned.example
    https:banned.example                      (no host)           banned.example
    https://\\banned.example                  \\banned.example    banned.example
    https://banned.example\\@good.example/    good.example        banned.example
    https://banned%2Eexample/                 banned%2eexample    banned.example
    https://bＡnned.example/  (fullwidth A)   bａnned.example     banned.example
    https://banned.example./                  banned.example.     banned.example.

A browser treats a backslash as a slash, skips any run of slashes after the scheme,
percent-decodes the host, and maps it through IDNA (NFKC, case folding, and dropping
characters such as a zero-width space). A trailing dot names the same DNS host. A label
IDNA refuses outright (a right-to-left letter followed by a left-to-right one) is refused
too rather than guessed at. Each row was stored, then linked from the post page, with
the admin's ban on banned.example never consulted.

`url_is_storable` refuses those shapes for the special schemes only. Other schemes
(`magnet:`, `file:`, `mailto:` ...) have no host a ban applies to and keep their old rule.
"""
import contextlib
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Post, Site
from app.utils import MAX_URL_HOST_LENGTH, url_is_storable
from tests.factories import make_community, make_domain, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'

HOST_MISMATCH = [
    'https:///banned.example/x',
    'https:////banned.example/x',
    'https:banned.example/x',
    'https:/banned.example/x',
    'https://\\banned.example/x',
    'https:\\\\banned.example/x',
    'https://banned.example\\@good.example/x',
    'http:///banned.example/x',
    'ws:///banned.example/x',
    'wss:///banned.example/x',
    'ftp:///banned.example/x',
    'HTTPS:///banned.example/x',
    ' https:///banned.example/x',
    'https://banned%2Eexample/x',
    'https://bＡnned.example/x',
    'https://banned.example./x',
    'https://banned..example/x',
    'https://ban\u200bned.example/x',
    'https://\u05d0a.example/x',
    # Characters newer than the Unicode 3.2 tables IDNA2003's nameprep uses. A browser
    # maps them by UTS46 on current Unicode: a squared A is 'a'; the shorthand format
    # control and a supplementary variation selector are dropped as default-ignorable.
    'https://b\U0001F130nned.example/x',
    'https://ban\U0001BCA0ned.example/x',
    'https://ban\U000E0100ned.example/x',
    # Unassigned in this Python's tables: a browser on newer Unicode may map it.
    'https://ban\u0378ned.example/x',
]

STILL_STORABLE = [
    'https://banned.example/x',
    'https://user:pw@good.example:8443/x?q=1#f',
    'https://bücher.example/x',
    'https://xn--bcher-kva.example/x',
    'https://[2001:db8::1]/x',
    'https://good.example/a%2Fb',
    'https://good.example/x\\..\\y',
    'https://good.example/x?next=https:///y',
    'file:///etc/hosts',
    'magnet:?xt=urn:btih:0123456789abcdef',
    'mailto:someone@example.test',
    'gemini://example.test/page',
]


class TestTheRule:
    @pytest.mark.parametrize('url', HOST_MISMATCH)
    def test_a_url_whose_browser_host_differs_from_the_ban_checks_is_not_storable(self, url):
        assert url_is_storable(url) is False

    @pytest.mark.parametrize('url', STILL_STORABLE)
    def test_urls_whose_host_both_read_alike_are_still_storable(self, url):
        assert url_is_storable(url) is True


# 253 characters, each label within DNS's 63: the longest host a DNS name can be
LONGEST_HOST = '.'.join(['a' * 63, 'b' * 63, 'c' * 63, 'd' * 61])


class TestTheHostLengthCap:
    """IDNA's nameprep costs about 10 microseconds a host character, and gallery_attachments asks for every
    attachment of a peer's object. No DNS name is longer than 253 characters, so a longer host is refused
    before any IDNA work, whatever the scheme."""

    def test_a_host_at_the_dns_limit_is_storable(self):
        assert len(LONGEST_HOST) == MAX_URL_HOST_LENGTH == 253
        assert url_is_storable(f'https://{LONGEST_HOST}/x') is True

    @pytest.mark.parametrize('url', [f'https://e{LONGEST_HOST}/x', f'gemini://e{LONGEST_HOST}/x',
                                     'https://' + 'a' * 100_000 + '/x'])
    def test_a_longer_host_is_refused_before_idna_reads_it(self, url, monkeypatch):
        def no_idna(label):
            raise AssertionError('nameprep read a host past the DNS length limit')
        monkeypatch.setattr('app.utils.nameprep', no_idna)

        assert url_is_storable(url) is False


@pytest.fixture(autouse=True)
def no_head(monkeypatch):
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    local = make_instance(HOST, software='piefed')
    make_user(local, 'founder', local=True)
    author = make_user(make_instance(PEER), 'author')
    author.ap_profile_id = f'https://{PEER}/u/author'
    community = make_community('general')
    domain = make_domain('banned.example')
    domain.banned = True
    db.session.commit()
    return SimpleNamespace(author=author, community=community)


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    class _Lock:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr('app.redis_client', _Lock())


def _create(env, url, slug):
    activity = {'id': f'https://{PEER}/activities/{slug}', 'type': 'Create',
                'actor': env.author.ap_profile_id,
                'object': {'id': f'https://{PEER}/p/{slug}', 'type': 'Page',
                           'name': 'A link post', 'attributedTo': env.author.ap_profile_id,
                           'audience': env.community.ap_profile_id,
                           'attachment': [{'type': 'Link', 'href': url}]}}
    return Post.new(env.author, env.community, activity)


class TestIngestingANewPost:
    @pytest.mark.parametrize('url', HOST_MISMATCH)
    def test_no_link_to_a_banned_domain_is_stored_through_a_host_mismatch(self, env, url):
        post = _create(env, url, slug=str(HOST_MISMATCH.index(url)))
        db.session.commit()

        assert post.url is None

    def test_the_plain_spelling_is_still_refused_by_the_ban(self, env):
        """The control: the ban itself still fires for the url the browser visits."""
        with pytest.raises(Exception, match='banned.example is blocked by admin'):
            _create(env, 'https://banned.example/x', slug='plain')
        db.session.rollback()
