"""The integrated providers, by kind."""
from app.payments.adapters.base import Adapter

_ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    _ADAPTERS[adapter.kind] = adapter


def adapter_for(kind: str) -> Adapter | None:
    return _ADAPTERS.get(kind)
