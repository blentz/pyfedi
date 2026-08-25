import re

import redis

from app.utils import create_captcha, decode_captcha, get_redis_connection, publish_sse_event


class TestCreateCaptcha:
    def test_returns_an_image_an_audio_clip_and_a_uuid(self, app, redis_double):
        result = create_captcha()

        assert re.fullmatch(r'[0-9a-f]{24}', result['uuid'])
        assert result['image'].startswith('data:image/jpeg;base64,')
        assert result['audio'].startswith('data:audio/wav;base64,')

    def test_the_stored_code_is_accepted_by_decode_captcha(self, app, redis_double):
        """The round trip both functions exist for.

        Fails if create_captcha stops storing the code, or stores it under a
        different key than decode_captcha reads.
        """
        result = create_captcha()
        stored = redis_double.get('captcha_' + result['uuid'])

        assert stored is not None
        assert decode_captcha(result['uuid'], stored) is True

    def test_the_requested_length_is_honoured(self, app, redis_double):
        result = create_captcha(length=6)
        assert len(redis_double.get('captcha_' + result['uuid'])) == 6

    def test_each_captcha_gets_its_own_uuid(self, app, redis_double):
        assert create_captcha()['uuid'] != create_captcha()['uuid']


class TestDecodeCaptcha:
    def test_a_wrong_code_is_rejected(self, app, redis_double):
        redis_double.set('captcha_' + 'b' * 24, 'WXYZ')
        assert decode_captcha('b' * 24, 'nope') is False

    def test_an_unknown_uuid_is_rejected(self, app, redis_double):
        assert decode_captcha('c' * 24, 'wxyz') is False

    def test_a_none_uuid_is_rejected_without_raising(self, app, redis_double):
        """The except TypeError arm: re.fullmatch(None) raises."""
        assert decode_captcha(None, 'wxyz') is False

    def test_a_none_code_against_a_live_captcha_is_rejected_without_raising(self, app, redis_double):
        """Regression guard: CaptchaField.post_validate passes self.data as
        code, which WTForms leaves as None when the captcha field is omitted
        from the submitted form. Without the `code is not None` guard,
        `code.lower()` raises AttributeError instead of failing validation
        cleanly -- distinct from the None-uuid case above, which is caught by
        the regex guard before any Redis lookup happens; this one requires a
        live, stored code so the code.lower() call is actually reached.
        """
        redis_double.set('captcha_' + 'd' * 24, 'WXYZ')
        assert decode_captcha('d' * 24, None) is False


class TestGetRedisConnection:
    def test_a_tcp_connection_string_is_parsed(self, app):
        """redis.Redis does not connect until a command is issued, so this
        builds a client without touching the network."""
        client = get_redis_connection('redis://localhost:6379/2')
        assert isinstance(client, redis.Redis)
        assert client.connection_pool.connection_kwargs['db'] == 2

    def test_a_unix_socket_connection_string_is_parsed(self, app):
        client = get_redis_connection('unix:///var/run/redis.sock?db=3')
        assert isinstance(client, redis.Redis)
        assert client.connection_pool.connection_kwargs['path'] == '/var/run/redis.sock'

    def test_no_argument_falls_back_to_configured_url(self, app):
        assert isinstance(get_redis_connection(), redis.Redis)


class TestPublishSseEvent:
    def test_publishes_to_the_named_channel(self, app, redis_double):
        """Fails if the key or value is dropped on the way to publish().

        Uses a real subscriber on the double rather than asserting a call.
        """
        pubsub = redis_double.pubsub()
        pubsub.subscribe('notifications:7')
        pubsub.get_message(timeout=1)          # the subscribe confirmation

        publish_sse_event('notifications:7', '{"num_notifs": 3}')

        message = pubsub.get_message(timeout=1)
        assert message['data'] == '{"num_notifs": 3}'
