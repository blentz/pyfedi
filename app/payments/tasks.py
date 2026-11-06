"""Calls to a provider that must not hold up a webhook or a page, and are worth retrying."""
from flask import current_app

from app import celery
from app.payments.adapters import adapter_for
from app.payments.adapters.base import ProviderError
from app.utils import get_task_session, patch_db_session

RETRY_SECONDS = 300
MAX_RETRIES = 12   # an hour of retries: long enough for a provider's outage, and failing loudly after


@celery.task(bind=True, max_retries=MAX_RETRIES, default_retry_delay=RETRY_SECONDS)
def cancel_at_provider_task(self, kind: str, reference: str):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                adapter_for(kind).cancel(reference)
        except ProviderError as error:
            current_app.logger.warning(f'Payments: {kind} cancel failed for {reference}: {error}')
            raise self.retry(exc=error)
        finally:
            session.close()


def cancel_at_provider(kind: str, reference: str) -> None:
    if current_app.debug:
        cancel_at_provider_task(kind, reference)
    else:
        cancel_at_provider_task.delay(kind, reference)
