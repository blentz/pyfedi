"""domain_from_url (app/utils.py:1442-1457) decides which Domain row a post is
attributed to. Domain carries `banned` (site-wide admin ban) and is the target
of DomainBlock (per-user block), so a defect here is a blocking defect.

Before the fix in this commit, line 1443 read
`urlparse(url.lower().replace('www.', ''))` -- a blanket replacement over the
whole URL string, before parsing, removing every occurrence rather than a
leading host label. `awww.evil.example` was recorded as `aevil.example`.
"""
from app.utils import canonical_host, domain_from_url, domain_name_from_url
from tests.factories import make_domain


class TestHostIsNotMangled:
    """Mutation that fails these: restoring `.replace('www.', '')` on the whole
    URL string at app/utils.py:1443."""

    def test_an_interior_www_is_not_stripped(self, app, db_session):
        domain = domain_from_url('https://awww.evil.example/post/1')
        assert domain.name == 'awww.evil.example'

    def test_two_distinct_hosts_do_not_collide(self, app, db_session):
        """The sharp one. Pre-fix both mangle to `aevil.example` and share one
        row, so banning either bans both."""
        first = domain_from_url('https://awww.evil.example/a')
        second = domain_from_url('https://aevil.example/a')
        assert first.id != second.id
        assert {first.name, second.name} == {'awww.evil.example', 'aevil.example'}


class TestLeadingWwwIsStripped:
    """The intended normalization, which the fix must preserve. Mutation that
    fails these: deleting the `startswith('www.')` strip."""

    def test_a_leading_www_is_removed(self, app, db_session):
        domain = domain_from_url('https://www.example.com/a')
        assert domain.name == 'example.com'

    def test_www_and_bare_host_share_one_row(self, app, db_session):
        bare = domain_from_url('https://example.com/a')
        with_www = domain_from_url('https://www.example.com/b')
        assert bare.id == with_www.id


class TestYoutubeAlias:
    """Mutation that fails this: deleting the `youtu.be` alias."""

    def test_youtu_be_is_recorded_as_youtube_com(self, app, db_session):
        domain = domain_from_url('https://youtu.be/dQw4w9WgXcQ')
        assert domain.name == 'youtube.com'


class TestCreateFlag:
    """Mutation that fails the first: changing `if create and domain is None`
    to `if domain is None`. Mutation that fails the second: dropping the
    `create` guard entirely."""

    def test_create_false_returns_none_when_no_row_exists(self, app, db_session):
        assert domain_from_url('https://never-seen.example/a', create=False) is None

    def test_create_false_returns_an_existing_row(self, app, db_session):
        existing = make_domain('seen.example')
        assert domain_from_url('https://seen.example/a', create=False).id == existing.id

    def test_create_true_makes_the_row(self, app, db_session):
        domain = domain_from_url('https://fresh.example/a')
        assert domain is not None and domain.name == 'fresh.example'


class TestUnparseable:
    """Mutation that fails these: deleting the `parsed_url.hostname` guard, so
    the function raises or records an empty name instead of returning None."""

    def test_a_url_with_no_host_returns_none(self, app, db_session):
        assert domain_from_url('not-a-url', create=False) is None

    def test_an_empty_string_returns_none(self, app, db_session):
        assert domain_from_url('', create=False) is None


class TestIpv6Literals:
    """Owner ruling 2026-10-13: an IPv6 literal is filed under ipaddress's compressed form, without brackets as
    urlparse gives every host, so the spellings a browser reads as one address share one row and one ban.
    Mutation that fails these: dropping canonical_host from domain_name_from_url."""

    def test_spellings_of_one_address_share_one_row(self, app, db_session):
        rows = [domain_from_url(url) for url in
                ('https://[::1]/a', 'https://[0:0::1]/b', 'https://[0:0:0:0:0:0:0:1]:8080/', 'http://[0::0:1]/')]
        assert {row.id for row in rows} == {rows[0].id}
        assert rows[0].name == '::1'

    def test_upper_case_hex_is_the_same_address(self, app, db_session):
        assert domain_from_url('https://[2001:DB8::0:1]/').name == '2001:db8::1'

    def test_a_host_that_is_no_address_is_left_as_it_is(self, app, db_session):
        assert domain_name_from_url('https://example.com:8080/') == 'example.com'
        assert domain_name_from_url('') is None
        assert domain_name_from_url('https://[::1/x') is None
        assert domain_name_from_url('mailto:a@example.com') is None


class TestCanonicalHost:
    def test_ipv6_literals_with_or_without_brackets(self):
        assert canonical_host('[0:0::1]') == canonical_host('0:0:0:0:0:0:0:1') == '::1'
        assert canonical_host('[::FFFF:7F00:1]') == canonical_host('::ffff:127.0.0.1')

    def test_anything_else_is_unchanged(self):
        for host in ('example.com', 'example.com:8080', '1.2.3.4', '[zzz]', 'a:b', ''):
            assert canonical_host(host) == host
