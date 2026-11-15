"""What a provider's call can fail with. Apart from the adapters package, so that a module the adapters themselves need
(the BTCPay connection and daily price) can raise them without importing that package."""


class InvalidWebhook(Exception):
    """The body is not from the provider, or isn't a body at all."""


class ProviderError(Exception):
    """The provider refused or could not be reached. Its text never includes a credential."""
