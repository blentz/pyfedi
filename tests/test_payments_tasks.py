"""Telling a provider to cancel happens outside the webhook, and is retried (plan phase 1, Task 4)."""
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from flask import current_app

from app.payments.adapters.base import ProviderError
from app.payments.tasks import cancel_at_provider, cancel_at_provider_task


def test_it_asks_the_providers_adapter(app):
    adapter = MagicMock()
    with app.app_context(), patch('app.payments.tasks.adapter_for', return_value=adapter) as adapter_for:
        cancel_at_provider_task.run('stripe', 'sub_1')
    adapter_for.assert_called_once_with('stripe')
    adapter.cancel.assert_called_once_with('sub_1')


def test_a_provider_failure_is_retried(app):
    adapter = MagicMock()
    adapter.cancel.side_effect = ProviderError('down')
    with app.app_context(), patch('app.payments.tasks.adapter_for', return_value=adapter), \
            patch.object(cancel_at_provider_task, 'retry', side_effect=Retry) as retry, pytest.raises(Retry):
        cancel_at_provider_task.run('stripe', 'sub_1')
    assert retry.called


@pytest.mark.parametrize('debug', [True, False], ids=['debug', 'production'])
def test_it_runs_in_the_background_unless_debugging(app, debug):
    with app.app_context(), patch.dict(current_app.config, {'DEBUG': debug}), \
            patch('app.payments.tasks.cancel_at_provider_task') as task:
        cancel_at_provider('stripe', 'sub_1')
    assert task.called is debug
    assert task.delay.called is not debug
