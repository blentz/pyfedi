"""app/activitypub/signature.py.

MEASUREMENT BASIS. The module stood at 70.66% on the full-suite --cov=app run
at 65a965089, carrying 70 missing statements and 50 missing arcs. The one file
that predates this one, tests/test_signature.py, is an import-time script of
bare asserts over recorded fixtures -- it covers calculate_digest,
parse_signature and compile_signature and nothing else, and it is left alone.

Two defects are pinned here and repaired together:

  P1  precheck raised ValueError for a malformed or empty Date header and
      TypeError for a well-formed one with no timezone. The inbox catches
      VerificationFormatError only (app/activitypub/routes.py:687-691), so all
      three were a 500 where a logged 400 was intended -- from any host that
      can reach the inbox.
  P2  signature_part split on '=' with no maxsplit, so a base64 value lost its
      padding; a comma-separated part with no '=' was an IndexError; and an
      absent Signature header was an AttributeError.

Both pins assert the exception TYPE rather than the fact of a raise, because
the unrepaired code raises too.
"""
import pytest

from app.activitypub.signature import (HttpSignature, VerificationFormatError,
                                       signature_part)

pytestmark = pytest.mark.usefixtures('site')


def _inbox_request(app, headers=None, body=b'{"type": "Create"}', with_digest=True):
    """A request shaped like the one the inbox hands precheck.

    The digest is computed from the body rather than hard-coded: precheck
    compares them (signature.py:387-389), so a stale constant would refuse
    every request here for the wrong reason.
    """
    sent = {'Content-Type': 'application/activity+json'}
    if with_digest:
        sent['Digest'] = HttpSignature.calculate_digest(body)
    sent.update(headers or {})
    return app.test_request_context('/inbox', method='POST', data=body, headers=sent)


# --------------------------------------------------------------------------
# P1: three Date headers that were 500s
# --------------------------------------------------------------------------


@pytest.mark.parametrize('date_header, why', [
    ('not a date', 'unparseable'),
    ('', 'empty'),
    ('Tue, 16 Sep 2026 12:00:00', 'well formed but carrying no timezone'),
])
def test_a_bad_date_header_is_a_format_error_not_a_crash(app, date_header, why):
    """Before the repair:

        PROBE s1 exception: ValueError Invalid date value or format "not a date"
        PROBE s3 exception: ValueError Invalid date value or format ""
        PROBE s2 exception: TypeError can't subtract offset-naive and
                            offset-aware datetimes

    The third is the interesting one: RFC 7231 requires the timezone, so a
    naive date IS a peer's bug -- but the answer to a peer's bug is the 400 the
    caller already knows how to log, not a traceback.
    """
    from flask import request
    with _inbox_request(app, {'Date': date_header}):
        with pytest.raises(VerificationFormatError):
            HttpSignature.precheck(request)


def test_a_naive_date_within_the_hour_is_accepted_as_utc(app):
    """The other half of P1's inversion. A repair that refused every naive date
    outright would pass the test above; this asserts the reading RFC 7231
    mandates -- no timezone means UTC -- and so keeps the repair honest.
    """
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from flask import request
    naive_now = format_datetime(datetime.now(timezone.utc)).replace(' +0000', '').replace(' GMT', '')

    with _inbox_request(app, {'Date': naive_now}):
        HttpSignature.precheck(request)


def test_a_date_an_hour_and_a_half_old_is_still_refused(app):
    """The guard precheck exists for, which the repair must not weaken."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from flask import request
    stale = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=90))

    with _inbox_request(app, {'Date': stale}):
        with pytest.raises(VerificationFormatError, match='Date is too far away'):
            HttpSignature.precheck(request)


def test_a_current_date_with_a_timezone_passes(app):
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from flask import request

    with _inbox_request(app, {'Date': format_datetime(datetime.now(timezone.utc))}):
        HttpSignature.precheck(request)


def test_a_missing_date_header_is_a_format_error(app):
    from flask import request

    with _inbox_request(app):
        with pytest.raises(VerificationFormatError, match='No date header present'):
            HttpSignature.precheck(request)


def test_a_missing_digest_header_is_a_format_error(app):
    from flask import request

    with _inbox_request(app, with_digest=False):
        with pytest.raises(VerificationFormatError, match='No digest header present'):
            HttpSignature.precheck(request)


def test_a_digest_that_does_not_match_the_body_is_a_format_error(app):
    """The digest of a DIFFERENT body, which is the shape a replayed or edited
    request has -- a constant like 'nonsense' would also fail the comparison
    but would not resemble anything an attacker sends.
    """
    from flask import request
    other = HttpSignature.calculate_digest(b'{"type": "Delete"}')

    with _inbox_request(app, {'Digest': other}):
        with pytest.raises(VerificationFormatError, match='Digest is incorrect'):
            HttpSignature.precheck(request)


# --------------------------------------------------------------------------
# P2: signature_part
# --------------------------------------------------------------------------


def test_a_value_keeps_everything_after_its_first_equals(app):
    """Before the repair `split('=')` with no maxsplit dropped a base64 value's
    padding: `PROBE s4 padded signature: 'YWJj'` for `signature="YWJj=="`.
    """
    header = 'keyId="https://remote.example/u/bob#main-key",signature="YWJj=="'

    assert signature_part(header, 'signature') == 'YWJj=='


def test_a_part_with_no_equals_is_skipped(app):
    """`PROBE s4 no equals exception: IndexError list index out of range`.

    A peer's Signature header is attacker-controlled, so a single stray comma
    reached this. The repair skips the part; the key that IS present must still
    be found afterwards, or skipping would have been indistinguishable from
    giving up.
    """
    assert signature_part('created,created=1758240000', 'created') == '1758240000'
    assert signature_part('nonsense', 'created') == ''


def test_an_absent_signature_header_gives_the_not_found_answer(app):
    """`PROBE s4 header absent exception: AttributeError 'NoneType' object has
    no attribute 'split'`. '' is what the function already returns when the key
    is not found, so the not-found path stays single.
    """
    assert signature_part(None, 'created') == ''
    assert signature_part('', 'created') == ''


def test_a_key_that_is_not_present_gives_an_empty_string(app):
    header = 'keyId="https://remote.example/u/bob#main-key",algorithm="rsa-sha256"'

    assert signature_part(header, 'created') == ''


def test_whitespace_around_the_key_and_the_value_is_stripped(app):
    """Peers pad their comma-separated parts differently; both strips are
    load-bearing and a single-sided fixture would leave one untested.
    """
    assert signature_part('algorithm="hs2019", created = 1758240000 ', 'created') == '1758240000'
