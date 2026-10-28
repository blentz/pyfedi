import httpx

from interop.clients import RetryAfterTransport


def _transport(*statuses, headers=None, patience=90):
    """A RetryAfterTransport over canned answers; `calls` counts the requests and `slept` the waits."""
    answers = iter(statuses)
    calls, slept = [], []

    def inner(request):
        calls.append(request)
        return httpx.Response(next(answers), headers=headers or {})

    return RetryAfterTransport(httpx.MockTransport(inner), patience=patience, sleep=slept.append), calls, slept


def _post(transport):
    return httpx.Client(transport=transport, base_url='https://piefed.test').post('/comment', json={})


def test_a_429_is_retried_after_the_retry_after_it_sends():
    transport, calls, slept = _transport(429, 200, headers={'Retry-After': '7'})
    assert _post(transport).status_code == 200
    assert (len(calls), slept) == (2, [7.0])


def test_a_429_without_retry_after_waits_five_seconds():
    transport, calls, slept = _transport(429, 429, 200)
    assert _post(transport).status_code == 200
    assert slept == [5.0, 5.0]


def test_other_statuses_are_not_retried():
    transport, calls, slept = _transport(400)
    assert _post(transport).status_code == 400
    assert (len(calls), slept) == (1, [])


def test_patience_is_bounded_and_the_last_429_is_returned():
    transport, calls, slept = _transport(429, 429, 429, headers={'Retry-After': '40'}, patience=60)
    assert _post(transport).status_code == 429
    assert slept == [40.0]


def test_a_retry_after_of_zero_still_waits_a_second_and_ends():
    transport, calls, slept = _transport(*[429] * 100, headers={'Retry-After': '0'}, patience=3)
    assert _post(transport).status_code == 429
    assert slept == [1.0, 1.0, 1.0]


def test_an_http_date_retry_after_waits_five_seconds():
    transport, calls, slept = _transport(429, 200, headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})
    assert _post(transport).status_code == 200
    assert slept == [5.0]
