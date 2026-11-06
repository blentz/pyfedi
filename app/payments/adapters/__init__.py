"""The integrated providers, by kind."""
from app.payments.adapters.base import Adapter
from app.payments.adapters.btcpay_adapter import BtcpayAdapter
from app.payments.adapters.paypal_adapter import PaypalAdapter
from app.payments.adapters.stripe_adapter import StripeAdapter

_ADAPTERS: dict[str, Adapter] = {StripeAdapter.kind: StripeAdapter(), BtcpayAdapter.kind: BtcpayAdapter(),
                                PaypalAdapter.kind: PaypalAdapter()}


def adapter_for(kind: str) -> Adapter | None:
    return _ADAPTERS.get(kind)
