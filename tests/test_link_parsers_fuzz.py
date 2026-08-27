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

A crash this fuzzing found, SINCE FIXED: none of the three functions caught
the ValueError that Python's own urllib.parse raises for certain malformed
netlocs -- an unbalanced IPv6 bracket ("Invalid IPv6 URL" / "'...' does not
appear to be an IPv4 or IPv6 address"), or a host that fails urllib's
NFKC-based homograph-confusability check ("netloc '...' contains invalid
characters under NFKC normalization"). All three called urlparse() on a
caller-supplied string with no try/except around it, so a submitted post link
containing e.g. "http://example.com[" raised out of domain_from_url,
remove_tracking_from_link and fixup_url alike, uncaught. The fix
(tests/test_urlparse_valueerror_guards.py, app/utils.py) makes each return
its own safe value instead: None, the url unchanged, and (url, url).

This file was written while that defect was still open, and it tolerated the
crash by message-matching it. That tolerance is now GONE for the urlparse
shapes: a ValueError of any kind out of remove_tracking_from_link or
fixup_url fails the test outright, and out of domain_from_url only the
still-open NUL-byte shape below is exempt. Re-introducing the defect turns
this file red, which is the point.

What each test does instead is check the guard's ANSWER. The oracle here has
to call urlparse itself to work out the expected host, so it hits the same
refusal -- and where it does, the test asserts the function under test
returned the documented safe value rather than propagating or inventing a
host. Those iterations are counted as `guarded`.

Still open, and still exempt for domain_from_url only: a hostname containing
a literal NUL character parses without error but is then rejected by the
Postgres driver when used as a query parameter ("A string literal cannot
contain NUL (0x00) characters"). That is a DB round trip and so cannot occur
in the other two, DB-free functions, and the parse guard is deliberately
scoped to urlparse() so as not to swallow it. It is written up in
docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md.

`_is_known_nul_byte_defect` matches by MESSAGE, not by catching `ValueError`
broadly, and that is deliberate: a blanket `except ValueError` would also
absorb any OTHER, unrelated `ValueError`-shaped defect domain_from_url might
have -- silently counting a genuinely new finding as more of this one. Only
the message shape named above is treated as this known, already-reported
refusal; anything else propagates and fails the test, which is what makes it
a real, new finding rather than noise from this harness.

No test asserts on how MANY inputs hit that remaining known defect. Doing so
(`assert skipped > 0`) would mean fixing it turns this fuzz suite red the day
someone acts on the report. Instead, each test asserts
`checked + guarded [+ skipped] [+ excluded] == ITERATIONS`: every generated
input was accounted for by exactly one path, which proves the loop ran and
the property was evaluated on every non-excluded input, without depending on
the defect still existing. `skipped` is still recorded and printed, so it
stays visible as data even though nothing asserts on its value.
"""
import random
from urllib.parse import urlparse

import atheris

from app.utils import domain_from_url, fixup_url, remove_tracking_from_link

ITERATIONS = 500

# The one ValueError shape still reported-but-unfixed: a NUL byte in the host
# survives urlparse and is refused by the Postgres driver instead. Reachable
# from domain_from_url only -- the other two functions never touch the DB.
#
# urlparse's own malformed-netloc refusals used to be listed here too. They
# were removed when app/utils.py grew guards for them: leaving them would mean
# this harness went on tolerating a defect that is now fixed.
_KNOWN_NUL_BYTE_DEFECT_MESSAGE_FRAGMENT = 'cannot contain NUL'


def _is_known_nul_byte_defect(exc: Exception) -> bool:
    """True only for the exact reported-not-fixed ValueError shape above.
    A DIFFERENT ValueError, or any other exception type, returns False here
    and is left to propagate out of the calling test -- see the module
    docstring for why matching by message rather than by type matters."""
    return (isinstance(exc, ValueError)
            and _KNOWN_NUL_BYTE_DEFECT_MESSAGE_FRAGMENT in str(exc))


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
        checked = guarded = skipped = 0
        for url in _fuzzed_urls(seed=1234, count=ITERATIONS):
            try:
                domain = domain_from_url(url, create=True)
            except ValueError as e:
                if not _is_known_nul_byte_defect(e):
                    raise
                skipped += 1
                continue

            try:
                hostname = urlparse(url.lower()).hostname
            except ValueError:
                # urlparse refuses this netloc outright, so there is no host
                # for the oracle to compare against. The property that remains
                # is the guard's: domain_from_url answered "no domain" rather
                # than propagating the ValueError or inventing a host.
                assert domain is None, (
                    f'host confusion: {url!r} is unparseable, but was recorded '
                    f'as {domain.name!r}')
                guarded += 1
                continue

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

        print(f'domain_from_url: {checked} checked, {guarded} guarded (unparseable '
              f'netloc, safe value asserted), {skipped} skipped (known NUL-byte defect)')
        # Defect-independent: every input was accounted for by exactly one
        # path, proving the loop ran and the property was evaluated -- this
        # holds whether or not the known NUL-byte defect still exists (see
        # the module docstring). checked > 0 additionally guards against an
        # over-broad match silently swallowing every iteration; guarded > 0
        # proves the generator still produces netlocs urlparse refuses, so the
        # guard branch above is genuinely exercised rather than vacuous.
        assert checked + guarded + skipped == ITERATIONS
        assert checked > 0
        assert guarded > 0


class TestRemoveTrackingFromLinkHasNoHostConfusion:
    """Every host except youtu.be must come back byte-for-byte identical
    (app/utils.py:3104-3105's `else: / return url`); youtu.be is the one
    documented alias, and only ever becomes youtube.com."""

    def test_no_unhandled_exception_and_no_host_confusion(self, app):
        checked = guarded = 0
        for url in _fuzzed_urls(seed=5678, count=ITERATIONS):
            # No exemption: this function no longer raises ValueError for any
            # input, so one escaping here is a real regression and fails.
            result = remove_tracking_from_link(url)

            try:
                input_host = urlparse(url).netloc
            except ValueError:
                # urlparse refuses this netloc, so there is no host for the
                # oracle to compare against. What remains is the guard's own
                # property: the url came back untouched.
                assert result == url, (
                    f'host confusion: unparseable link {url!r} was altered to {result!r}')
                guarded += 1
                continue

            if input_host == 'youtu.be':
                result_host = urlparse(result).netloc
                assert result_host == 'youtube.com', (
                    f'host confusion: youtu.be link {url!r} rewritten to '
                    f'host {result_host!r}, not youtube.com')
            else:
                assert result == url, (
                    f'host confusion: non-youtu.be link {url!r} was altered to {result!r}')
            checked += 1

        print(f'remove_tracking_from_link: {checked} checked, {guarded} guarded '
              f'(unparseable netloc, safe value asserted)')
        assert checked + guarded == ITERATIONS
        assert checked > 0
        assert guarded > 0


class TestFixupUrlParsingHasNoHostConfusion:
    """Scoped to the YouTube URL matrix only -- the peertube branch
    (app/utils.py:3115-3129) performs a DB query and a live HTTP GET, and is
    excluded by construction (the `len(url) > 25 and url[-25:][:3] == '/w/'`
    guard below), matching the brief's "fixup_url's parsing"."""

    _YOUTUBE_DOMAINS = {'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
                        'youtube.com', 'youtu.be'}

    def test_no_unhandled_exception_and_no_host_confusion(self, app):
        checked = guarded = excluded = 0
        for url in _fuzzed_urls(seed=91011, count=ITERATIONS):
            if len(url) > 25 and url[-25:][:3] == '/w/':
                excluded += 1
                continue

            # No exemption: this function no longer raises ValueError for any
            # input, so one escaping here is a real regression and fails.
            with app.test_request_context('/'):
                thumbnail_url, embed_url = fixup_url(url)

            try:
                input_host = urlparse(url).netloc
            except ValueError:
                # urlparse refuses this netloc, so there is no host for the
                # oracle to compare against. What remains is the guard's own
                # property: both slots pass the url through untouched.
                assert thumbnail_url == url and embed_url == url, (
                    f'host confusion: unparseable link {url!r} was rewritten to '
                    f'thumbnail={thumbnail_url!r} embed={embed_url!r}')
                guarded += 1
                continue

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

        print(f'fixup_url: {checked} checked, {guarded} guarded (unparseable netloc, '
              f'safe value asserted), {excluded} excluded (peertube-shaped, DB/network)')
        # Every generated input took exactly one of three paths: checked
        # against the full property, checked against the guard's passthrough
        # property, or excluded as peertube-shaped.
        assert checked + guarded + excluded == ITERATIONS
        assert checked > 0
        assert guarded > 0
