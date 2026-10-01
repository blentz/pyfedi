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
import httpx
import pytest

from app import db
from app.activitypub.signature import (HttpSignature, VerificationFormatError,
                                       signature_part)
from app.models import SendQueue, utcnow

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


# --------------------------------------------------------------------------
# post_request: the outbound delivery task
# --------------------------------------------------------------------------


class _Response:
    """The parts of an httpx response post_request reads.

    `close` is counted because the task calls it on every path that reaches a
    response, and a leak there is invisible to every other assertion.
    """

    def __init__(self, status_code=200, text='', payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


def _deliver(response=None, raises=None, **kwargs):
    """Run the task with HttpSignature.signed_request doubled."""
    from unittest.mock import patch
    from app.activitypub.signature import post_request
    body = kwargs.pop('body', {'type': 'Create', 'id': 'https://local.example/activities/1'})
    call = dict(uri='https://remote.example/inbox', body=body, private_key='a-key',
                key_id='https://local.example/u/alice#main-key')
    call.update(kwargs)
    with patch('app.activitypub.signature.HttpSignature.signed_request') as signed:
        if raises is not None:
            signed.side_effect = raises
        else:
            signed.return_value = response if response is not None else _Response()
        post_request(**call)
    return signed


def _log():
    from app.models import ActivityPubLog
    return ActivityPubLog.query.one()


def test_a_delivered_activity_is_logged_as_a_success(app, db_session):
    from app import db
    response = _Response(status_code=200)

    _deliver(response)

    db.session.expire_all()
    log = _log()
    assert log.direction == 'out'
    assert log.activity_type == 'Create'
    assert log.activity_id == 'https://local.example/activities/1'
    assert log.result == 'success'
    assert response.closed is True


def test_an_activity_without_a_context_is_given_the_default_one(app, db_session):
    """signature.py:100-101. The stored activity_json is where the addition is
    visible, since the body dict itself is the caller's.
    """
    import json
    from app import db

    _deliver(_Response(), body={'type': 'Follow', 'id': 'https://local.example/activities/2'})

    db.session.expire_all()
    sent = json.loads(_log().activity_json)
    assert '@context' in sent


def test_an_activity_that_brings_its_own_context_keeps_it(app, db_session):
    """The guard's other arm: a caller-supplied context is not overwritten."""
    import json
    from app import db

    _deliver(_Response(), body={'type': 'Follow', 'id': 'https://local.example/activities/3',
                                '@context': ['https://example.test/ns']})

    db.session.expire_all()
    assert json.loads(_log().activity_json)['@context'] == ['https://example.test/ns']


def test_an_activity_with_no_type_is_logged_with_an_empty_type(app, db_session):
    from app import db

    _deliver(_Response(), body={'id': 'https://local.example/activities/4'})

    db.session.expire_all()
    assert _log().activity_type == ''


@pytest.mark.parametrize('uri', ['', None])
def test_an_empty_destination_is_a_failure_without_a_request(app, db_session, uri):
    from app import db

    signed = _deliver(_Response(), uri=uri)

    db.session.expire_all()
    log = _log()
    assert log.result == 'failure'
    assert log.exception_message == 'empty uri'
    assert signed.call_count == 0


@pytest.mark.parametrize('status', [202, 204])
def test_the_accepted_codes_are_successes_that_say_so(app, db_session, status):
    """signature.py:136-139 appends the code to the message on top of the uri,
    and both are successes -- the message is the only thing distinguishing them
    from a 200.
    """
    from app import db

    _deliver(_Response(status_code=status))

    db.session.expire_all()
    log = _log()
    assert log.result == 'success'
    assert log.exception_message.endswith(f' {status}')


def test_a_plain_error_code_is_a_failure_carrying_the_body(app, db_session):
    from app import db

    _deliver(_Response(status_code=403, text='forbidden'))

    db.session.expire_all()
    log = _log()
    assert log.result == 'failure'
    assert log.exception_message.startswith('403: forbidden')
    assert log.exception_message.endswith('https://remote.example/inbox')


def test_an_html_error_page_is_ignored_rather_than_failed(app, db_session):
    """A peer answering an inbox POST with a login page is not a federation
    failure worth keeping, so the task downgrades it to 'ignored'.
    """
    from app import db

    _deliver(_Response(status_code=500, text='<!DOCTYPE html><html>oops</html>'))

    db.session.expire_all()
    log = _log()
    assert log.result == 'ignored'
    assert log.exception_message.startswith('500: HTML instead of JSON response')


def test_a_community_with_no_followers_there_has_its_membership_repaired(app, db_session):
    """The branch that calls fix_local_community_membership. The local rows for
    that instance's users go; a user on ANOTHER instance keeps theirs, which is
    what keeps the instance filter load-bearing.
    """
    from app import db
    from app.models import CommunityMember
    from tests.factories import make_community, make_instance, make_user

    local = make_instance('test.piefed.local', software='piefed')
    burn = make_user(local, 'burnseat', local=True)
    assert burn.id == 1
    remote_instance = make_instance('remote.example', software='lemmy')
    other_instance = make_instance('elsewhere.example', software='lemmy')
    community = make_community('microblogs')
    community.private_key = 'a-key'
    theirs = make_user(remote_instance, 'theirs', local=False)
    elsewhere = make_user(other_instance, 'elsewhere', local=False)
    db.session.add(CommunityMember(user_id=theirs.id, community_id=community.id))
    db.session.add(CommunityMember(user_id=elsewhere.id, community_id=community.id))
    db.session.commit()

    _deliver(_Response(status_code=400, text='{"error":"community_has_no_followers"}'))

    db.session.expire_all()
    remaining = [m.user_id for m in CommunityMember.query.all()]
    assert theirs.id not in remaining
    assert elsewhere.id in remaining


def test_a_banned_sender_is_handed_to_the_ban_processor(app, db_session):
    from unittest.mock import patch
    from app import db

    with patch('app.activitypub.util.process_banned_message') as processor:
        _deliver(_Response(status_code=400, text='{"error":"person_is_banned_from_site"}',
                           payload={'error': 'person_is_banned_from_site'}))

    db.session.expire_all()
    assert processor.call_count == 1
    assert processor.call_args.args[0] == {'error': 'person_is_banned_from_site'}
    assert processor.call_args.args[1] == 'remote.example'


@pytest.mark.parametrize('status', [410, 418])
def test_a_gone_instance_is_marked_and_its_queue_emptied(app, db_session, status):
    """410 and 418 both mean never send here again (signature.py:126-131), so
    the instance is flagged AND whatever is queued for it is dropped. The queue
    row for a DIFFERENT domain must survive, or the delete's filter proves
    nothing.
    """
    from datetime import datetime
    from app import db
    from app.models import Instance, SendQueue
    from tests.factories import make_instance, make_user

    local = make_instance('test.piefed.local', software='piefed')
    burn = make_user(local, 'burnseat', local=True)
    assert burn.id == 1
    gone = make_instance('remote.example', software='lemmy')
    db.session.add(SendQueue(destination='https://remote.example/inbox',
                             destination_domain='remote.example', actor='a', private_key='k',
                             payload='{}', retries=0, send_after=utcnow()))
    db.session.add(SendQueue(destination='https://elsewhere.example/inbox',
                             destination_domain='elsewhere.example', actor='a', private_key='k',
                             payload='{}', retries=0, send_after=utcnow()))
    db.session.commit()

    _deliver(_Response(status_code=status, text='gone'))

    db.session.expire_all()
    assert db.session.get(Instance, gone.id).gone_forever is True
    assert [q.destination_domain for q in SendQueue.query.all()] == ['elsewhere.example']


@pytest.mark.parametrize('failure', [
    httpx.ConnectError('connection refused'),
    httpx.ConnectTimeout('connection refused'),
    httpx.HTTPError('connection refused'),
])
def test_a_transport_failure_is_logged_and_retried_like_a_5xx(app, db_session, failure):
    """D767, fixed (owner ruling 2026-09-30). The inner handler records
    http_status_code = 404, and the retry gate admitted only 429 and >= 500, so
    a peer refusing connections was dropped after one attempt while a 502 was
    retried for hours. A transport failure now joins the same backoff queue.
    The plain `httpx.HTTPError` row is what production raises: `signed_request`
    re-wraps every httpx error as one (signature.py, its `except httpx.HTTPError`).
    """
    post_request_retries = 3
    _deliver(raises=failure, retries=post_request_retries)

    db.session.expire_all()
    log = _log()
    assert log.result == 'failure'
    # the exception's own text, not merely the prefix: a mutant dropping
    # str(e) leaves an operator with 'could not send:' and nothing else
    assert log.exception_message == 'could not send:connection refused'
    queued = SendQueue.query.one()
    assert queued.destination == 'https://remote.example/inbox'
    assert queued.retries == post_request_retries
    assert queued.retry_reason == 'could not send:connection refused'


def test_a_failure_before_sending_is_not_retried(app, db_session):
    """D767's boundary: only transport failures retry. `signed_request` raising
    ValueError for a uri it refuses to fetch will refuse it again next time.
    """
    _deliver(raises=ValueError('URI is invalid'))

    db.session.expire_all()
    assert _log().result == 'failure'
    assert SendQueue.query.count() == 0


@pytest.mark.parametrize('status', [429, 500, 503])
def test_a_retryable_code_is_queued_with_exponential_backoff(app, db_session, status):
    from app import db
    from app.models import SendQueue

    _deliver(_Response(status_code=status, text='later'), retries=3)

    db.session.expire_all()
    queued = SendQueue.query.one()
    assert queued.destination == 'https://remote.example/inbox'
    assert queued.destination_domain == 'remote.example'
    assert queued.actor == 'https://local.example/u/alice#main-key'
    assert queued.retries == 3
    assert queued.retry_reason.startswith(f'{status}: later')


def test_the_backoff_is_capped_at_four_hours(app, db_session):
    """`backoff = min(60 * (2 ** retries), 15360)` -- ten retries would be
    seventeen hours without the cap, so the two rows differ by whether the cap
    binds.
    """
    from datetime import datetime, timedelta
    from app import db
    from app.models import SendQueue

    _deliver(_Response(status_code=500, text='later'), retries=10)

    db.session.expire_all()
    queued = SendQueue.query.one()
    assert queued.send_after <= utcnow() + timedelta(seconds=15361)
    assert queued.send_after >= utcnow() + timedelta(seconds=15000)


def test_a_retryable_code_on_another_content_type_is_not_queued(app, db_session):
    """The queue is for activity delivery only (signature.py:156), so the same
    500 on any other content type is dropped.
    """
    from app import db
    from app.models import SendQueue

    _deliver(_Response(status_code=500, text='later'), content_type='application/ld+json')

    db.session.expire_all()
    assert SendQueue.query.count() == 0


# --------------------------------------------------------------------------
# The helpers around the signing itself
# --------------------------------------------------------------------------


def test_the_http_date_defaults_to_now_and_accepts_an_epoch(app):
    """Both arms of `if epoch_seconds is None` (signature.py:56-58). The given
    epoch is a fixed instant, so the formatting is asserted exactly rather than
    by shape.
    """
    from app.activitypub.signature import http_date

    assert http_date(0) == 'Thu, 01 Jan 1970 00:00:00 GMT'
    assert http_date().endswith('GMT')


def test_an_ld_date_is_parsed_without_microseconds_and_none_stays_none(app):
    """parse_ld_date drops microseconds deliberately -- peers differ on whether
    they send them, and a comparison against a stored value has to agree. Both
    arms of the None guard are rows here.
    """
    from app.activitypub.signature import format_ld_date, parse_ld_date

    parsed = parse_ld_date('2026-09-18T12:34:56.789000Z')

    assert parsed.microsecond == 0
    assert parsed.year == 2026 and parsed.hour == 12
    assert parse_ld_date(None) is None
    assert format_ld_date(parsed).endswith('Z')


def test_a_delivery_goes_through_celery_unless_debug_or_told_otherwise(app):
    """send_post_request's whole body is the choice between .delay and a direct
    call (signature.py:85-91), so both arms are rows -- and the direct one
    asserts the arguments arrive, since a call that dropped them would pass a
    bare "it was called".
    """
    from unittest.mock import patch
    from app.activitypub.signature import send_post_request

    with patch('app.activitypub.signature.post_request') as task:
        send_post_request('https://remote.example/inbox', {'id': 'x'}, 'k', 'kid',
                          new_task=True)
        assert task.delay.call_count == 1
        assert task.call_count == 0
        assert task.delay.call_args.kwargs['uri'] == 'https://remote.example/inbox'

    with patch('app.activitypub.signature.post_request') as task:
        send_post_request('https://remote.example/inbox', {'id': 'x'}, 'k', 'kid',
                          new_task=False)
        assert task.call_count == 1
        assert task.delay.call_count == 0


def test_a_deserialized_key_is_cached_and_reused(app):
    """The cache is keyed on the SHA-256 of the PEM, so the second call must
    return the very same object -- equality would pass even with no cache.
    """
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, public_key = RsaKeys.generate_keypair()
    HttpSignature._private_key_cache.clear()
    HttpSignature._public_key_cache.clear()

    first = HttpSignature._get_private_key_instance(private_key)
    second = HttpSignature._get_private_key_instance(private_key)
    public_first = HttpSignature._get_public_key_instance(public_key)
    public_second = HttpSignature._get_public_key_instance(public_key)

    assert first is second
    assert public_first is public_second
    assert len(HttpSignature._private_key_cache) == 1
    assert len(HttpSignature._public_key_cache) == 1


@pytest.mark.parametrize('cache_name, loader', [
    ('_private_key_cache', '_get_private_key_instance'),
    ('_public_key_cache', '_get_public_key_instance'),
])
def test_a_full_key_cache_drops_its_older_half(app, cache_name, loader):
    """signature.py's eviction is "remove the first half when full", which only
    runs on a MISS against a full cache. The cache is filled with cheap
    placeholder entries rather than real keys -- a thousand RSA deserializations
    would cost minutes, and the eviction never looks at the values.
    """
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, public_key = RsaKeys.generate_keypair()
    cache = getattr(HttpSignature, cache_name)
    cache.clear()
    for i in range(HttpSignature._cache_max_size):
        cache[f'placeholder-{i}'] = object()

    getattr(HttpSignature, loader)(private_key if 'private' in cache_name else public_key)

    # half the placeholders gone, plus the real entry just added
    assert len(cache) == HttpSignature._cache_max_size // 2 + 1
    assert 'placeholder-0' not in cache
    assert f'placeholder-{HttpSignature._cache_max_size - 1}' in cache
    cache.clear()


def test_the_signing_payload_names_every_header_the_peer_asked_for(app):
    """headers_from_request's six arms in one request: the request target, the
    two pseudo-headers read out of the Signature header, content-type,
    content-length, and an ordinary header through the generic branch.
    """
    from flask import request
    from app.activitypub.signature import HttpSignature

    signature = 'keyId="https://remote.example/u/bob#main-key",created=1758240000,expires=1758243600'
    with app.test_request_context('/inbox', method='POST', data=b'{}',
                                  headers={'Signature': signature,
                                           'Content-Type': 'application/activity+json',
                                           'Host': 'test.piefed.local'}):
        payload = HttpSignature.headers_from_request(
            request, ['(request-target)', '(created)', '(expires)', 'content-type',
                      'content-length', 'host'])

    assert payload.splitlines() == [
        '(request-target): post /inbox',
        '(created): 1758240000',
        '(expires): 1758243600',
        'content-type: application/activity+json',
        'content-length: 2',
        'host: test.piefed.local',
    ]


def test_a_header_the_request_does_not_carry_signs_as_empty(app):
    """The generic branch's default. A peer listing a header it did not send
    still has to produce a payload, and the empty string is what the signature
    was computed over on its side too.
    """
    from flask import request
    from app.activitypub.signature import HttpSignature

    with app.test_request_context('/inbox', method='POST', data=b'{}'):
        payload = HttpSignature.headers_from_request(request, ['digest'])

    assert payload == 'digest: '


def test_a_signature_header_missing_an_item_is_a_verification_error(app):
    """parse_signature's KeyError handler names what it DID find, so the
    assertion reads the message rather than only the type -- the message is the
    only thing that tells an operator which peer sent what.
    """
    from app.activitypub.signature import HttpSignature, VerificationError

    with pytest.raises(VerificationError, match='Missing item from details'):
        HttpSignature.parse_signature('keyId="https://remote.example/u/bob#main-key"')


def test_a_uri_with_no_scheme_is_refused_before_anything_is_sent(app):
    from app.activitypub.signature import HttpSignature

    with pytest.raises(ValueError, match='URI does not contain a scheme'):
        HttpSignature.signed_request('remote.example/inbox', {'id': 'x'}, 'k', 'kid')


def test_an_async_request_returns_the_parts_instead_of_sending(app):
    """send_via_async hands the pieces to the notifs service rather than
    performing the request (signature.py:505-507), so the assertion is that the
    signature was built AND that nothing was sent.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()

    with patch('app.activitypub.signature.httpx_client') as client:
        uri, headers, body_bytes = HttpSignature.signed_request(
            'https://remote.example/inbox', {'id': 'x'}, private_key,
            'https://local.example/u/alice#main-key', send_via_async=True)

    assert uri == 'https://remote.example/inbox'
    assert 'Signature' in headers
    assert b'"id": "x"' in body_bytes or b'"id":"x"' in body_bytes
    assert client.request.call_count == 0


def test_a_document_with_no_signature_section_is_a_format_error(app):
    from app.activitypub.signature import LDSignature, VerificationFormatError

    with pytest.raises(VerificationFormatError, match='Invalid signature section'):
        LDSignature.verify_signature({'id': 'https://remote.example/activities/1'}, 'key')


def test_a_document_signed_with_an_unknown_type_is_a_format_error(app):
    from app.activitypub.signature import LDSignature, VerificationFormatError

    document = {'id': 'https://remote.example/activities/1',
                'signature': {'type': 'Ed25519Signature2018',
                              'creator': 'https://remote.example/u/bob#main-key',
                              'created': '2026-09-18T12:00:00Z',
                              'signatureValue': 'YWJj'}}

    with pytest.raises(VerificationFormatError, match='Unknown signature type'):
        LDSignature.verify_signature(document, 'key')


def test_the_default_context_grows_when_the_site_asks_for_the_full_one(app):
    from app.activitypub.signature import default_context

    app.config['FULL_AP_CONTEXT'] = False
    assert default_context() == ['https://www.w3.org/ns/activitystreams',
                                 'https://w3id.org/security/v1']

    app.config['FULL_AP_CONTEXT'] = True
    full = default_context()
    assert full[:2] == ['https://www.w3.org/ns/activitystreams',
                        'https://w3id.org/security/v1']
    assert isinstance(full[2], dict) and 'lemmy' in full[2]


@pytest.mark.parametrize('missing', ['community', 'instance'])
def test_the_membership_repair_needs_both_a_community_and_an_instance(app, db_session, missing):
    """Both false arms of `if community and instance`. Each row removes exactly
    one of the two, so neither operand can be dropped without a survivor.
    """
    from app import db
    from app.activitypub.signature import fix_local_community_membership
    from app.models import CommunityMember
    from app.utils import get_task_session
    from tests.factories import make_community, make_instance, make_user

    local = make_instance('test.piefed.local', software='piefed')
    burn = make_user(local, 'burnseat', local=True)
    assert burn.id == 1
    remote_instance = make_instance('remote.example', software='lemmy')
    community = make_community('microblogs')
    community.private_key = 'a-key'
    member = make_user(remote_instance, 'theirs', local=False)
    db.session.add(CommunityMember(user_id=member.id, community_id=community.id))
    db.session.commit()

    private_key = 'no-such-key' if missing == 'community' else 'a-key'
    uri = ('https://remote.example/inbox' if missing == 'community'
           else 'https://nowhere.example/inbox')
    session = get_task_session()
    try:
        fix_local_community_membership(uri, private_key, session)
        session.commit()
    finally:
        session.close()

    db.session.expire_all()
    assert CommunityMember.query.count() == 1


# --------------------------------------------------------------------------
# Verification and the request the signing produces
# --------------------------------------------------------------------------


def _signed_headers(app, private_key, key_id, body=b'{"type": "Create"}', algorithm='rsa-sha256'):
    """Sign a request the way a peer does, so verify_request has something real
    to check rather than a hand-built string.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature
    # the destination cannot be a .local host: is_invalid_get_request_uri
    # (app/utils.py:5503) rejects those outright unless the app is in debug
    with patch('app.activitypub.signature.httpx_client'):
        uri, headers, sent = HttpSignature.signed_request(
            'https://remote.example/inbox', None, private_key, key_id,
            send_via_async=True)
    return headers


def test_a_request_with_no_signature_header_is_a_format_error(app):
    from flask import request
    from app.activitypub.signature import HttpSignature, VerificationFormatError

    with app.test_request_context('/inbox', method='POST', data=b'{}'):
        with pytest.raises(VerificationFormatError, match='No signature header present'):
            HttpSignature.verify_request(request, 'key')


def test_an_unknown_signature_algorithm_is_refused(app):
    """rsa-sha256 and hs2019 are the two the module accepts (signature.py:423-427),
    so this asserts the refusal AND the two acceptances are covered elsewhere --
    a mutant dropping either operand has a row on the other side.
    """
    from flask import request
    from app.activitypub.signature import HttpSignature, VerificationFormatError

    header = ('keyId="https://remote.example/u/bob#main-key",algorithm="ed25519",'
              'headers="(request-target) host date",signature="YWJj"')
    with app.test_request_context('/inbox', method='POST', data=b'{}',
                                  headers={'Signature': header}):
        with pytest.raises(VerificationFormatError, match='Unknown signature algorithm'):
            HttpSignature.verify_request(request, 'key')


@pytest.mark.parametrize('algorithm', ['rsa-sha256', 'hs2019'])
def test_a_genuinely_signed_request_verifies_under_both_algorithms(app, algorithm):
    """The end-to-end shape: sign with signed_request, verify with
    verify_request, against a real keypair. Both accepted algorithm names are
    rows, which is what stops a mutant dropping one of the two comparisons.
    """
    from flask import request
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, public_key = RsaKeys.generate_keypair()
    key_id = 'https://remote.example/u/bob#main-key'
    headers = _signed_headers(app, private_key, key_id)
    headers['Signature'] = headers['Signature'].replace('algorithm="rsa-sha256"',
                                                        f'algorithm="{algorithm}"')

    # the same method the signing used: (request-target) carries it, so a GET
    # context against a post-signed header is a genuine mismatch
    with app.test_request_context('/inbox', method='POST', headers=headers):
        assert HttpSignature.verify_request(request, public_key) is True


def test_a_signature_from_the_wrong_key_is_a_mismatch(app):
    """The guard the whole module exists for: a well-formed signature made with
    a DIFFERENT key must fail, and it must fail as VerificationError rather
    than as a crash.
    """
    from flask import request
    from app.activitypub.signature import HttpSignature, RsaKeys, VerificationError

    private_key, _ = RsaKeys.generate_keypair()
    _, other_public_key = RsaKeys.generate_keypair()
    headers = _signed_headers(app, private_key, 'https://remote.example/u/bob#main-key')

    with app.test_request_context('/inbox', method='POST', headers=headers):
        with pytest.raises(VerificationError, match='Signature mismatch'):
            HttpSignature.verify_request(request, other_public_key)


def test_an_invalid_get_uri_is_refused(app):
    """The second of signed_request's two url guards. is_invalid_get_request_uri
    is patched rather than fed a magic string, because WHICH uris it rejects is
    its own function's business and this test is about the guard.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature

    with patch('app.activitypub.signature.is_invalid_get_request_uri', return_value=True):
        with pytest.raises(ValueError, match='URI is invalid'):
            HttpSignature.signed_request('https://remote.example/inbox', {'id': 'x'},
                                         'k', 'kid')


def test_a_get_request_signs_no_body_and_asks_for_json_ld(app):
    """signed_request's body-less arm (signature.py:473-478): no digest, no
    content type, an Accept header instead, and an empty payload.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()

    with patch('app.activitypub.signature.httpx_client'):
        uri, headers, body_bytes = HttpSignature.signed_request(
            'https://remote.example/outbox', None, private_key, 'kid',
            method='get', send_via_async=True)

    assert body_bytes == b''
    assert 'Digest' not in headers
    assert headers['Accept'] == 'application/ld+json'


def test_a_transport_error_is_reraised_with_the_url(app):
    """signature.py:517-519 rewraps httpx's error so the log names the url;
    the assertion reads the message, which is the only thing the rewrap adds.
    """
    from unittest.mock import patch
    import httpx
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()
    failure = httpx.ConnectError('refused')
    failure.request = httpx.Request('POST', 'https://remote.example/inbox')

    with patch('app.activitypub.signature.httpx_client') as client:
        client.request.side_effect = failure
        with pytest.raises(httpx.HTTPError, match='HTTP Exception for https://remote.example/inbox'):
            HttpSignature.signed_request('https://remote.example/inbox', {'id': 'x'},
                                         private_key, 'kid')


@pytest.mark.parametrize('status', [400, 403, 404, 410, 418, 500])
@pytest.mark.parametrize('method', ['post', 'POST'])
def test_a_status_is_always_returned_rather_than_raised(app, status, method):
    """D1322. This used to read
    `test_a_client_error_on_a_post_is_raised_but_404_and_5xx_are_returned`, and
    it pinned a branch that raised ValueError for a 4xx POST -- reaching it by
    passing `method='POST'`, which NO CALLER DOES: the parameter is
    `Literal["get", "post"]` and every call site in app/ passes it lowercase or
    not at all, so the comparison `method == "POST"` was never true in
    production and the branch had never run.

    It is gone rather than corrected to `.lower()`, because `post_request` reads
    those 4xx responses: 410 and 418 mark the peer gone forever and empty its
    SendQueue, `community_has_no_followers` repairs the membership, and
    `person_is_banned_from_site` processes the ban. A raise here lands in that
    function's `except Exception`, which records `http_status_code = 404` and
    does none of them.

    Both spellings of the method are asserted, so the removal holds for the
    entry point the old test used as well as the one callers use.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.content = b'body'

        def close(self):
            pass

    with patch('app.activitypub.signature.httpx_client') as client:
        client.request.return_value = _Resp(status)
        result = HttpSignature.signed_request('https://remote.example/inbox',
                                              {'id': 'x'}, private_key, 'kid',
                                              method=method)

    assert result.status_code == status


def test_a_signed_get_is_the_same_request_without_a_body(app):
    """signed_get_request is two lines and they are both delegation, so the
    assertion is on what it passes through -- a None body and the get method.
    """
    from unittest.mock import patch
    from app.activitypub.signature import signed_get_request

    with patch('app.activitypub.signature.HttpSignature.signed_request') as signed:
        signed.return_value = 'the response'
        result = signed_get_request('https://remote.example/u/bob', 'k', 'kid')

    assert result == 'the response'
    assert signed.call_args.args[1] is None
    assert signed.call_args.args[5] == 'get'


def test_an_ld_signature_round_trips(app, no_network_ld_signing):
    """create_signature and verify_signature against each other, which is the
    only way to cover the creation side without a recorded peer document.

    `no_network_ld_signing` because `jsonld.normalize` resolves this document's
    `@context` through pyld's default loader, which reaches the real internet via
    `requests` -- respx does not touch it, and conftest's docstring names this as
    a gap each test must close for itself. Without the fixture these two tests
    fetched `https://www.w3.org/ns/activitystreams` live on every run and failed
    intermittently under full-suite load; they were the only two calling
    `jsonld.normalize` without it.
    """
    from app.activitypub.signature import LDSignature, RsaKeys

    private_key, public_key = RsaKeys.generate_keypair()
    document = {'@context': 'https://www.w3.org/ns/activitystreams',
                'id': 'https://local.example/activities/1', 'type': 'Create',
                'actor': 'https://local.example/u/alice'}

    signature = LDSignature.create_signature(document, private_key,
                                             'https://local.example/u/alice#main-key')
    signed_document = dict(document, signature=signature)

    assert signature['type'] == 'RsaSignature2017'
    assert LDSignature.verify_signature(signed_document, public_key) is None
    # The fixture yields the URLs it served, so "no network" is asserted rather
    # than assumed -- an empty list would mean the loader was bypassed.
    assert no_network_ld_signing


def test_an_ld_signature_made_with_another_key_is_a_mismatch(app,
                                                             no_network_ld_signing):
    """`no_network_ld_signing` for the same reason as the test above."""
    from app.activitypub.signature import LDSignature, RsaKeys, VerificationError

    private_key, _ = RsaKeys.generate_keypair()
    _, other_public_key = RsaKeys.generate_keypair()
    document = {'@context': 'https://www.w3.org/ns/activitystreams',
                'id': 'https://local.example/activities/1', 'type': 'Create'}
    signature = LDSignature.create_signature(document, private_key, 'kid')

    with pytest.raises(VerificationError, match='Signature mismatch'):
        LDSignature.verify_signature(dict(document, signature=signature), other_public_key)


def test_an_unknown_instance_returning_gone_logs_without_a_row_to_flag(app, db_session):
    """post_request's 410 branch reads `if existing_instance:` -- an instance
    this server has never recorded still has its queue cleared and still logs a
    failure, and nothing raises. That false arm is its own row.
    """
    from app import db
    from app.models import Instance

    _deliver(_Response(status_code=410, text='gone'))

    db.session.expire_all()
    assert Instance.query.filter_by(domain='remote.example').count() == 0
    assert _log().result == 'failure'


def test_a_task_that_cannot_log_rolls_back_and_reraises(app, db_session):
    """The outer handler. A body json.dumps cannot serialise raises before the
    log row is added, which is the cheapest way left to reach it now that a
    body with no `id` is refused cleanly (D768)."""
    from app.models import ActivityPubLog

    with pytest.raises(TypeError):
        _deliver(_Response(), body={'type': 'Create', 'id': 'https://local.example/activities/1',
                                    'unserialisable': object()})

    assert ActivityPubLog.query.count() == 0


@pytest.mark.parametrize('body', [None, {'type': 'Create'}, {'type': 'Create', 'id': None}])
def test_an_activity_with_no_id_is_logged_as_a_failure_and_not_sent(app, db_session, body):
    """D768, fixed: post_request read `'@context' not in body` and `body['id']`
    unguarded, so a None body raised TypeError and a body with no id raised
    KeyError out of the task, leaving no log row. Neither is deliverable, so
    it is now logged as a failure and nothing is sent."""
    signed = _deliver(_Response(), body=body)

    signed.assert_not_called()
    log = _log()
    assert log.result == 'failure'
    assert log.exception_message == 'no activity id, not sent: https://remote.example/inbox'


def test_an_unknown_digest_algorithm_is_refused(app):
    """calculate_digest's else arm. The only caller passes the default, so this
    is the one way the branch is reachable at all.
    """
    from app.activitypub.signature import HttpSignature

    with pytest.raises(ValueError, match='Unknown digest algorithm sha-512'):
        HttpSignature.calculate_digest(b'{}', algorithm='sha-512')


def test_a_body_that_already_carries_a_context_is_signed_unchanged(app):
    """signed_request repeats post_request's context check on its own body
    (signature.py:467-469), so it needs its own two rows -- the task's tests
    cannot reach this copy.
    """
    from unittest.mock import patch
    import json
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()
    body = {'id': 'x', '@context': ['https://example.test/ns']}

    with patch('app.activitypub.signature.httpx_client'):
        uri, headers, body_bytes = HttpSignature.signed_request(
            'https://remote.example/inbox', body, private_key, 'kid', send_via_async=True)

    assert json.loads(body_bytes)['@context'] == ['https://example.test/ns']


@pytest.mark.parametrize('scenario', ['unknown status', 'transport failure'])
def test_debug_logging_names_the_destination(app, db_session, scenario):
    """Two `if current_app.debug` arms inside post_request (signature.py:134-135,
    :146-147) that only run with debug on. The assertion is on the LOGGED TEXT:
    a mutant swapping the two messages would pass a bare "logger was called".
    """
    from unittest.mock import patch
    import httpx

    app.debug = True
    try:
        with patch.object(app.logger, 'error') as logged:
            if scenario == 'unknown status':
                _deliver(_Response(status_code=451, text='unavailable for legal reasons'))
            else:
                _deliver(raises=httpx.ConnectError('refused'))
    finally:
        app.debug = False

    message = logged.call_args.args[0]
    assert 'https://remote.example/inbox' in message
    if scenario == 'unknown status':
        assert message.startswith('Response code for post attempt to')
        assert '451' in message
    else:
        assert message.startswith('Exception while sending post to')


# --------------------------------------------------------------------------
# Rows added to close mutation survivors
# --------------------------------------------------------------------------


def test_a_date_an_hour_and_a_half_in_the_FUTURE_is_refused(app):
    """precheck's window is `abs(...) > 3600`, and without a future date the
    abs() is load-bearing for nothing -- a clock-skewed or forged peer sending
    tomorrow's date passes a one-sided comparison.
    """
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from flask import request
    from app.activitypub.signature import HttpSignature, VerificationFormatError
    ahead = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=90))

    with _inbox_request(app, {'Date': ahead}):
        with pytest.raises(VerificationFormatError, match='Date is too far away'):
            HttpSignature.precheck(request)


def test_a_debug_server_sends_in_process_even_when_a_task_was_asked_for(app):
    """`if current_app.debug or new_task is False:` -- the debug operand only
    shows with new_task left TRUE, which is what a normal caller passes.
    """
    from unittest.mock import patch
    from app.activitypub.signature import send_post_request

    app.debug = True
    try:
        with patch('app.activitypub.signature.post_request') as task:
            send_post_request('https://remote.example/inbox', {'id': 'x'}, 'k', 'kid',
                              new_task=True)
    finally:
        app.debug = False

    assert task.call_count == 1
    assert task.delay.call_count == 0


def test_the_ban_handler_runs_only_for_a_400(app, db_session):
    """`elif result.status_code == 400 and 'person_is_banned_from_site' in ...`
    -- the same body under a 403 must NOT reach the handler, or the status
    operand is load-bearing for nothing.
    """
    from unittest.mock import patch
    from app import db

    with patch('app.activitypub.util.process_banned_message') as processor:
        _deliver(_Response(status_code=403, text='{"error":"person_is_banned_from_site"}',
                           payload={'error': 'person_is_banned_from_site'}))

    db.session.expire_all()
    assert processor.call_count == 0
    assert _log().result == 'failure'


def test_a_client_error_on_a_GET_is_returned_rather_than_raised(app):
    """signed_request raises for a 4xx only when the method is POST
    (signature.py:520-523). A GET that 400s comes back as a response for the
    caller to read, which is what a webfinger lookup against a hostile peer
    looks like.
    """
    from unittest.mock import patch
    from app.activitypub.signature import HttpSignature, RsaKeys

    private_key, _ = RsaKeys.generate_keypair()

    class _Resp:
        status_code = 400
        content = b'nope'

    with patch('app.activitypub.signature.httpx_client') as client:
        client.request.return_value = _Resp()
        result = HttpSignature.signed_request('https://remote.example/u/bob', None,
                                              private_key, 'kid', method='get')

    assert result.status_code == 400
