"""`task_selector` -- the dispatcher every task in this package routes through.

`app/shared/tasks/__init__.py:61-68`. Twenty statements, and until this file
existed it sat at 83.3333% with two missing: `:63`'s debug override and `:68`'s
SYNCHRONOUS dispatch arm.

WHY THOSE TWO AND NOT THE OTHERS. `tests/conftest.py` sets `task_always_eager`,
so `:66`'s `.delay()` already runs synchronously in every test that goes
through this function -- which is exactly why `:66` was covered and `:68` was
not. And `current_app.debug` is False under test, so `:63` never ran.

THE TWO ARMS ARE INDISTINGUISHABLE BY SIDE EFFECT AND DISTINGUISHABLE ONLY BY
RETURN VALUE. Under eager Celery both `:66` and `:68` execute the task
synchronously, so "the task ran" is true under either. `:66` falls off the end
and returns None; `:68` returns the task's own return value. Every test here
asserts on the RETURN, because asserting the task ran would pass under both
arms -- the vacuous shape this campaign exists to catch.

HOW THE STUB REACHES THE DICT. `task_selector` builds `tasks` from imports
performed INSIDE the function body, so those names resolve at call time.
Patching `app.shared.tasks.flags.report_post` before the call therefore puts
the stub into the dict. The stub cannot be used to exercise `:66`, which calls
`.delay()` on a Celery task object a plain function does not have -- but `:66`
needs no test, having been covered incidentally all along.
"""

from types import SimpleNamespace

import pytest
from flask import current_app

from app.shared.tasks import task_selector

_SENTINEL = object()


def _stub_report_post(monkeypatch):
    """Replace `report_post` in ITS OWN MODULE with a recording stub.

    Returns `SimpleNamespace(calls=[])`; the stub appends its kwargs and
    returns `_SENTINEL`, which is what makes `:68`'s return observable.

    THE PATCH TARGET IS `app.shared.tasks.flags`, not `app.shared.tasks`.
    `task_selector` does `from app.shared.tasks.flags import report_post`
    inside its own body, so the name is looked up on the flags module when the
    call happens. Patching `app.shared.tasks.report_post` would not exist to
    patch, and patching the local `tasks` dict is impossible -- it is rebuilt
    on every call.
    """
    record = SimpleNamespace(calls=[])

    def _stub(**kwargs):
        record.calls.append(kwargs)
        return _SENTINEL

    monkeypatch.setattr('app.shared.tasks.flags.report_post', _stub)
    return record


def test_send_async_false_dispatches_synchronously_and_returns(
        db_session, monkeypatch):
    """`:68`, reached by `send_async=False`.

    THE RETURN IS THE ASSERTION. `:66` returns None; `:68` returns the task's
    value. Asserting only that the stub was called would pass under either arm,
    because `task_always_eager` makes `.delay()` synchronous too.
    """
    record = _stub_report_post(monkeypatch)

    result = task_selector('report_post', send_async=False, user_id=1,
                           post_id=2, summary='spam', instance_ids=[])

    assert result is _SENTINEL
    assert record.calls == [{'send_async': False, 'user_id': 1, 'post_id': 2,
                             'summary': 'spam', 'instance_ids': []}]


def test_debug_forces_synchronous_dispatch_despite_send_async_true(
        db_session, monkeypatch):
    """`:63`, and the arc `(62,63)`.

    THIS TEST IS ALSO A `:68` TEST, AND THAT IS THE POINT. `:63` sets
    `send_async = False`, so `:65` then takes the else arm. Passing
    `send_async=True` and still getting `_SENTINEL` back is what proves the
    override happened -- with debug False, the same call would take `:66` and
    return None.

    The stub records what it was called with, so the assertion also pins that
    the OVERRIDDEN value is what reaches the task, not the caller's `True`.

    Flask's `debug` is a property backed by `config['DEBUG']` (see
    `flask.Flask.debug`'s getter/setter in Flask 3.1.3), so setting the config
    key is what `current_app.debug` actually reads at `:62`.
    """
    record = _stub_report_post(monkeypatch)
    monkeypatch.setitem(current_app.config, 'DEBUG', True)

    result = task_selector('report_post', send_async=True, user_id=1,
                           post_id=2, summary='spam', instance_ids=[])

    assert result is _SENTINEL
    assert record.calls[0]['send_async'] is False


def test_an_unknown_task_key_raises_KeyError(db_session, monkeypatch):
    """`tasks[task_key]` with a key the dict does not carry.

    A dispatcher's failure mode for a typo'd key, asserted by a NATURAL raise
    rather than an injected one. This is the behaviour that makes a
    misspelled key loud at the call site instead of silently doing nothing.
    """
    _stub_report_post(monkeypatch)

    with pytest.raises(KeyError):
        task_selector('report_psot', send_async=False, user_id=1)
