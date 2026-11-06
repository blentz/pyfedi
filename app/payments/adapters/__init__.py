"""The integrated providers, by kind."""
from app.payments.adapters.base import Adapter
from app.payments.adapters.stripe_adapter import StripeAdapter

_ADAPTERS: dict[str, Adapter] = {StripeAdapter.kind: StripeAdapter()}


def adapter_for(kind: str) -> Adapter | None:
    return _ADAPTERS.get(kind)
