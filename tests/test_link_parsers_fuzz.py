"""Fuzzes the three link-parsing functions covered by this sub-project:
domain_from_url (app/utils.py:1442-1457), remove_tracking_from_link
(app/utils.py:3083-3105), and the YouTube URL matrix half of fixup_url
(app/utils.py:3110-3167 -- only "fixup_url's parsing" per this task's brief,
never the peertube branch at 3115-3129, which performs a DB query and a live
HTTP GET and is excluded by construction below).

The property, per the brief, is checkable without an oracle: no unhandled
exception, and no host confusion -- whatever host ends up in a function's
output must be traceable to the host that went in (at most the documented
`www.` strip / `youtu.be` -> `youtube.com` alias), never some other host.
"It did not crash" alone is not asserted, for the same reason the module
docstring in tests/fuzz/harnesses.py gives: a function that silently returned
garbage would still "not crash".

Why atheris.FuzzedDataProvider without atheris.Fuzz(). The on-demand campaign
(tests/fuzz/run_campaign.py) is coverage-guided libFuzzer, which installs its
own bytecode tracing and must never run under `--cov` (see "Fuzzing" in
tests/README.md) -- and this task's ratchet step measures coverage over the
WHOLE tests/ directory, which collects this file. So this harness only uses
FuzzedDataProvider as a structured random-bytes-to-values decoder, seeded from
a fixed-seed stdlib `random` stream for reproducibility, and iterates a fixed
number of times as an ordinary (fast, deterministic) pytest test -- not a
libFuzzer session. It finds real bugs (see "A crash this fuzzing found" below)
without ever fighting coverage.py's own tracing.

Iteration counts: 500 per function (1500 total). Chosen to be comfortably
under pytest.ini's 60s per-test timeout with wide margin -- measured at well
under a second per function on this environment, including domain_from_url's
DB round trips -- while still being high enough to reliably hit every
generator branch below (youtu.be aliasing, www-stripping, malformed-host
edge cases) many times over. This is not a coverage-guided search, so there
is no benefit to pushing the count higher without also switching to the
on-demand campaign machinery.

A crash this fuzzing found, reported per this task's brief ("report it; do
not fix it. A new defect found at this stage is a finding for the owner, not
a sixth task"): none of the three functions catches the ValueError that
Python's own urllib.parse raises for certain malformed netlocs -- an
unbalanced IPv6 bracket ("Invalid IPv6 URL" / "'...' does not appear to be an
IPv4 or IPv6 address"), or a host that fails urllib's NFKC-based
homograph-confusability check ("netloc '...' contains invalid characters
under NFKC normalization"). All three functions call urlparse() on a caller-
supplied string with no try/except around it, so a submitted post link
containing e.g. "http://example.com[" raises out of domain_from_url,
remove_tracking_from_link and fixup_url alike, uncaught. domain_from_url has
a second, narrower variant of the same shape: a hostname containing a literal
NUL character parses without error but is then rejected by the Postgres
driver when used as a query parameter ("A string literal cannot contain NUL
(0x00) characters"), which is a DB round trip and so cannot occur in the
other two, DB-free functions. This finding is deliberately NOT fixed here --
see `_is_known_urlparse_defect` below for how it is kept from turning this
file red -- and is written up in
docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md.

`_is_known_urlparse_defect` matches by MESSAGE, not by catching `ValueError`
broadly, and that is deliberate: a blanket `except ValueError` would also
absorb any OTHER, unrelated `ValueError`-shaped defect these functions might
have -- silently counting a genuinely new finding as more of this one. Only
the message shapes named above are treated as this known, already-reported
refusal; anything else propagates and fails the test, which is what makes it
a real, new finding rather than noise from this harness.

None of the three tests below asserts on how MANY inputs hit that known
defect. Doing so (`assert skipped > 0`) would mean fixing the urlparse
`ValueError` -- the exact "report it, do not fix it" finding above -- turns
this fuzz suite red the day someone acts on the report. Instead, each test
asserts `checked + skipped [+ excluded] == ITERATIONS`: every generated input
was accounted for by exactly one path, which proves the loop ran and the
property was evaluated on every non-excluded input, without depending on the
defect still existing. If the urlparse defect is ever fixed, `skipped` drops
to 0, `checked` rises to take its place, and the invariant still holds
unchanged -- nothing here needs editing. `skipped` is still recorded and
printed per test, so it stays visible as data even though nothing asserts on
its value.
"""
import random
from urllib.parse import urlparse

import atheris

from app.utils import domain_from_url, fixup_url, remove_tracking_from_link

ITERATIONS = 500

# Message fragments from the ValueError shapes this task's fuzzing found and
# reported (see "A crash this fuzzing found" above) -- urlparse's own
# malformed-netloc refusals, plus (domain_from_url only) a NUL byte reaching
# the Postgres driver as a query parameter. Not fixed in app/utils.py.
_KNOWN_URLPARSE_DEFECT_MESSAGE_FRAGMENTS = (
    'Invalid IPv6 URL',
    'does not appear to be an IPv4 or IPv6 address',
    'contains invalid characters under NFKC normalization',
    'cannot contain NUL',
)


def _is_known_urlparse_defect(exc: Exception) -> bool:
    """True only for the exact reported-not-fixed ValueError shapes above.
    A DIFFERENT ValueError, or any other exception type, returns False here
    and is left to propagate out of the calling test -- see the module
    docstring for why matching by message rather than by type matters."""
    return isinstance(exc, ValueError) and any(
        fragment in str(exc) for fragment in _KNOWN_URLPARSE_DEFECT_MESSAGE_FRAGMENTS)


def _random_url(fdp: atheris.FuzzedDataProvider) -> str:
    """Builds a plausible-shaped URL from fuzzer-controlled bytes.

    Deliberately biased rather than fully unstructured: a few branches force
    the alias/strip hosts (youtu.be, www.<random>, a bare youtube.com) so
    those code paths are hit often, alongside branches that hand urlparse
    arbitrary unicode -- which is what surfaces the malformed-host crash
    class documented above.
    """
    scheme = fdp.PickValueInList(['http', 'https', 'ftp', ''])
    host_kind = fdp.ConsumeIntInRange(0, 4)
    if host_kind == 0:
        host = 'youtu.be'
    elif host_kind == 1:
        host = 'www.' + fdp.ConsumeUnicodeNoSurrogates(10)
    elif host_kind == 2:
        host = 'youtube.com'
    elif host_kind == 3:
        host = fdp.PickValueInList(
            ['m.youtube.com', 'music.youtube.com', 'www.youtube.com'])
    else:
        host = fdp.ConsumeUnicodeNoSurrogates(20)
    path = fdp.ConsumeUnicodeNoSurrogates(20)
    query = fdp.ConsumeUnicodeNoSurrogates(20)
    url = f'{scheme}://{host}/{path}'
    if query:
        url += '?' + query
    return url


def _fuzzed_urls(seed: int, count: int):
    rng = random.Random(seed)
    for _ in range(count):
        data = rng.randbytes(rng.randint(0, 80))
        yield _random_url(atheris.FuzzedDataProvider(data))


class TestDomainFromUrlHasNoHostConfusion:
    """The property this sub-project's Task 1 fix exists to guarantee: the
    recorded Domain.name is the parsed hostname, at most `www.`-stripped and
    at most youtu.be->youtube.com aliased -- never a different host, and
    never a byproduct of string-level mangling (the pre-fix defect: a
    blanket `.replace('www.', '')` over the whole URL, not just a leading
    host label -- see tests/test_domain_from_url.py)."""

    def test_no_unhandled_exception_and_no_host_confusion(self, app, db_session):
        checked = skipped = 0
        for url in _fuzzed_urls(seed=1234, count=ITERATIONS):
            try:
                domain = domain_from_url(url, create=True)
            except ValueError as e:
                if not _is_known_urlparse_defect(e):
                    raise
                skipped += 1
                continue

            hostname = urlparse(url.lower()).hostname
            if hostname is None:
                assert domain is None
                checked += 1
                continue

            expected = hostname
            if expected.startswith('www.'):
                expected = expected[4:]
            if expected == 'youtu.be':
                expected = 'youtube.com'

            assert domain is not None, (
                f'a parseable hostname {hostname!r} produced no Domain row for {url!r}')
            assert domain.name == expected, (
                f'host confusion: {url!r} parsed to hostname {hostname!r} but '
                f'was recorded as {domain.name!r}, expected {expected!r}')
            checked += 1

        print(f'domain_from_url: {checked} checked, {skipped} skipped (known urlparse defect)')
        # Defect-independent: every input was accounted for by exactly one
        # path, proving the loop ran and the property was evaluated -- this
        # holds whether or not the known urlparse defect still exists (see
        # the module docstring). checked > 0 additionally guards against an
        # over-broad match silently swallowing every iteration.
        assert checked + skipped == ITERATIONS
        assert checked > 0


class TestRemoveTrackingFromLinkHasNoHostConfusion:
    """Every host except youtu.be must come back byte-for-byte identical
    (app/utils.py:3104-3105's `else: / return url`); youtu.be is the one
    documented alias, and only ever becomes youtube.com."""

    def test_no_unhandled_exception_and_no_host_confusion(self, app):
        checked = skipped = 0
        for url in _fuzzed_urls(seed=5678, count=ITERATIONS):
            try:
                result = remove_tracking_from_link(url)
            except ValueError as e:
                if not _is_known_urlparse_defect(e):
                    raise
                skipped += 1
                continue

            input_host = urlparse(url).netloc
            if input_host == 'youtu.be':
                result_host = urlparse(result).netloc
                assert result_host == 'youtube.com', (
                    f'host confusion: youtu.be link {url!r} rewritten to '
                    f'host {result_host!r}, not youtube.com')
            else:
                assert result == url, (
                    f'host confusion: non-youtu.be link {url!r} was altered to {result!r}')
            checked += 1

        print(f'remove_tracking_from_link: {checked} checked, {skipped} skipped (known urlparse defect)')
        assert checked + skipped == ITERATIONS
        assert checked > 0


class TestFixupUrlParsingHasNoHostConfusion:
    """Scoped to the YouTube URL matrix only -- the peertube branch
    (app/utils.py:3115-3129) performs a DB query and a live HTTP GET, and is
    excluded by construction (the `len(url) > 25 and url[-25:][:3] == '/w/'`
    guard below), matching the brief's "fixup_url's parsing"."""

    _YOUTUBE_DOMAINS = {'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
                        'youtube.com', 'youtu.be'}

    def test_no_unhandled_exception_and_no_host_confusion(self, app):
        checked = skipped = excluded = 0
        for url in _fuzzed_urls(seed=91011, count=ITERATIONS):
            if len(url) > 25 and url[-25:][:3] == '/w/':
                excluded += 1
                continue

            try:
                with app.test_request_context('/'):
                    thumbnail_url, embed_url = fixup_url(url)
            except ValueError as e:
                if not _is_known_urlparse_defect(e):
                    raise
                skipped += 1
                continue

            input_host = urlparse(url).netloc
            if input_host not in self._YOUTUBE_DOMAINS:
                assert thumbnail_url == url and embed_url == url, (
                    f'host confusion: non-YouTube link {url!r} was rewritten '
                    f'to thumbnail={thumbnail_url!r} embed={embed_url!r}')
            else:
                # Every non-passthrough output must resolve to a host in the
                # same YouTube family -- a fixup can rewrite the FORM of a
                # YouTube link (e.g. adding a canonical youtu.be thumbnail),
                # but never redirect it to an unrelated host.
                for candidate in (thumbnail_url, embed_url):
                    if candidate in ('', url):
                        continue
                    candidate_host = urlparse(candidate).netloc
                    assert candidate_host in self._YOUTUBE_DOMAINS, (
                        f'host confusion: YouTube link {url!r} produced '
                        f'{candidate!r}, resolving to host {candidate_host!r}')
            checked += 1

        print(f'fixup_url: {checked} checked, {skipped} skipped (known urlparse defect), '
              f'{excluded} excluded (peertube-shaped, DB/network)')
        # Every generated input took exactly one of three paths: checked
        # against the property, matched the known urlparse defect, or excluded
        # as peertube-shaped. Defect-independent for the same reason as the
        # other two tests -- fixing the urlparse ValueError only moves inputs
        # from `skipped` to `checked`.
        assert checked + skipped + excluded == ITERATIONS
        assert checked > 0
