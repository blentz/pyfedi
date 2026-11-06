"""What a provider adapter tells core, in nobody's vocabulary but ours. No provider payload gets past an adapter."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Paid:
    reference: str
    amount_minor: int
    currency: str
    user_id: int | None = None
    offer_id: int | None = None


@dataclass(frozen=True)
class Refunded:
    reference: str


@dataclass(frozen=True)
class SubscriptionStarted:
    reference: str
    user_id: int | None
    offer_id: int | None


@dataclass(frozen=True)
class SubscriptionEnded:
    reference: str


@dataclass(frozen=True)
class CustomerKnown:
    """The provider's id for this member, which later events with no account in them are matched by (plan, P2)."""
    user_id: int | None
    customer: str
