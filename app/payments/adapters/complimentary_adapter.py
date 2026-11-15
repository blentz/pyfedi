"""Complimentary membership (plan 2026-11-15-complimentary-membership.md, K1): given by admins, never bought. Its row is
all there is, so the code that looks a subscription's provider up (cancelling on account deletion) finds nothing to do."""
from datetime import datetime

from app.complimentary import KIND
from app.models import utcnow
from app.payments.adapters.base import InvalidWebhook, ProviderError


class ComplimentaryAdapter:
    kind = KIND
    prepaid = False   # nothing runs out: it has no end date

    def available(self) -> bool:
        return False   # never offered at checkout, and there is no webhook to answer

    def on_onion(self) -> bool:
        return False

    def checkout(self, offer, user, success_url: str, cancel_url: str, discount=None) -> str:
        raise ProviderError('complimentary membership is given, not bought')

    def parse_webhook(self, body: bytes, headers) -> tuple[str, list]:
        raise InvalidWebhook('complimentary membership has no webhook')

    def cancel(self, reference: str) -> None:
        return None

    def cancel_at_period_end(self, reference: str) -> datetime:
        return utcnow()

    def billing_portal_url(self, customer: str, return_url: str) -> str:
        raise ProviderError('complimentary membership has no billing')

    def describe_price(self, price_id: str) -> dict:
        raise ProviderError('complimentary membership has no price')

    def check_credentials(self, values: dict) -> None:
        raise ProviderError('complimentary membership has no credentials')
