"""Gifts (plan phase 2, G3): a free period of a tier, given by a code. Nothing is charged and nobody is asked, but a gift
is a subscription like the others: prepaid, so the daily sweep ends it and the reminder warns before it does."""
from datetime import datetime

from app.models import utcnow
from app.payments.adapters.base import InvalidWebhook, ProviderError


class GiftAdapter:
    kind = 'gift'
    prepaid = True

    def available(self) -> bool:
        return False   # never offered at checkout, and there is no webhook to answer

    def on_onion(self) -> bool:
        return False

    def checkout(self, offer, user, success_url: str, cancel_url: str, discount=None) -> str:
        raise ProviderError('a gift is given by a code, not bought')

    def parse_webhook(self, body: bytes, headers) -> tuple[str, list]:
        raise InvalidWebhook('gifts have no webhook')

    def cancel(self, reference: str) -> None:
        return None   # nobody to tell: its row is all there is

    def cancel_at_period_end(self, reference: str) -> datetime:
        return utcnow()

    def billing_portal_url(self, customer: str, return_url: str) -> str:
        raise ProviderError('a gift has no billing')

    def describe_price(self, price_id: str) -> dict:
        raise ProviderError('a gift has no price')

    def check_credentials(self, values: dict) -> None:
        raise ProviderError('a gift has no credentials')
