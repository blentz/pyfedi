"""What every integrated provider implements (spec, Adapter contract)."""
from collections.abc import Callable
from datetime import datetime
from typing import Protocol


class InvalidWebhook(Exception):
    """The body is not from the provider, or isn't a body at all."""


class ProviderError(Exception):
    """The provider refused or could not be reached. Its text never includes a credential."""


class Adapter(Protocol):
    kind: str
    available: Callable[[], bool]
    checkout: Callable[..., str]   # (offer, user, success_url, cancel_url) -> where to send the member
    parse_webhook: Callable[..., tuple[str, list]]   # (body, headers) -> (event id, neutral events), or InvalidWebhook
    cancel: Callable[[str], None]   # (reference); already gone counts as done
    cancel_at_period_end: Callable[[str], datetime]   # (reference) -> when it will end
    billing_portal_url: Callable[[str, str], str]   # (customer, return_url)
    describe_price: Callable[[str], dict]   # (price id) -> {'amount_minor', 'currency', 'interval'}
    check_credentials: Callable[[dict], None]   # raises ProviderError if the provider refuses them
