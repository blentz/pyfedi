"""What a provider adapter tells core, in nobody's vocabulary but ours. No provider payload gets past an adapter."""
from dataclasses import dataclass
from datetime import datetime


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
    renews_at: datetime | None = None   # naive UTC: the provider's next billing date, when it renews by itself


@dataclass(frozen=True)
class Renewed:
    """A subscription that renews by itself is paid through this naive UTC time: the provider's next billing date."""
    reference: str
    paid_through: datetime


@dataclass(frozen=True)
class SubscriptionEnded:
    reference: str


@dataclass(frozen=True)
class CustomerKnown:
    """The provider's id for this member, which later events with no account in them are matched by (plan, P2)."""
    user_id: int | None
    customer: str
