"""Each fixture proved by driving real application code through it.

A test asserting that fakeredis stores what you put in it tests fakeredis. These
drive PieFed's own functions and assert on what those functions do.
"""

import boto3
import pytest

from app.instance.util import bulk_follow
from app.models import ActivityPubLog, UserFollower
from app.utils import decode_captcha
from tests.factories import make_instance, make_site, make_user


def test_redis_double_backs_a_real_captcha_round_trip(app, redis_double):
    """decode_captcha reads back what generate stored, through the double."""
    uuid = 'a' * 24
    redis_double.set('captcha_' + uuid, 'WXYZ', ex=1800)

    assert decode_captcha(uuid, 'wxyz') is True, 'comparison is case-insensitive'
    assert decode_captcha(uuid, 'wxyz') is False, 'the code is consumed on use'


def test_redis_double_rejects_a_malformed_uuid(app, redis_double):
    """The regex guard rejects before any Redis call."""
    assert decode_captcha('not-a-uuid', 'wxyz') is False


def test_s3_bucket_fixture_provides_a_usable_bucket(app, s3_bucket):
    """The bucket exists and is empty, so a test can assert on what code puts in it."""
    client = boto3.client('s3')
    listing = client.list_objects_v2(Bucket=s3_bucket)

    assert listing['ResponseMetadata']['HTTPStatusCode'] == 200
    assert listing.get('KeyCount', 0) == 0


def test_delay_runs_the_task_inline(app, db_session, federation_peer):
    """task_always_eager, proved by effect: .delay() does the work in-process.

    Without eager mode this call would enqueue on the real broker and return an
    AsyncResult, leaving no row for this test to find.
    """
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True)
    federation_peer('wakko@mastodon.cloud')

    bulk_follow.delay(local.id, ['wakko@mastodon.cloud'])

    assert UserFollower.query.filter_by(local_user_id=local.id,
                                        is_inward=False).count() == 1


def test_delay_reraises_a_failing_task(app, db_session, federation_peer):
    """task_eager_propagates: a task that raises must fail the test, not vanish.

    Without it the exception is captured in the EagerResult and .delay() returns
    normally, so a broken task would look like a passing one.
    """
    make_instance('test.piefed.local', software='piefed')
    make_site()

    with pytest.raises(Exception):
        bulk_follow.delay(999999, ['wakko@mastodon.cloud'])


def test_delivery_can_be_proved_when_the_sender_has_keys(app, db_session, federation_peer):
    """The recipe tests/README.md gives for asserting an activity was delivered.

    Two things must both hold or the POST never happens: include_inbox=True, so
    respx has a route to match and assert_all_called can prove it was hit; and a
    sender with a real keypair, since signing dereferences the private key. With
    either missing this fails -- keyless at signing, routeless at teardown.
    """
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True, with_keys=True)
    federation_peer('wakko@mastodon.cloud', include_inbox=True)

    bulk_follow.delay(local.id, ['wakko@mastodon.cloud'])

    log = ActivityPubLog.query.filter_by(activity_type='Follow').one()
    assert log.result == 'success', log.exception_message
