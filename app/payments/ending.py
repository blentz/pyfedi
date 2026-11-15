"""When a cancelled or refunded subscription ends (plan 2026-11-15-btc-daily-price.md, B6): at the end of its current
subscription month, counted from its start, or when what was paid for runs out if that is sooner. Never mid-month, and
never past the month."""
import calendar
from datetime import datetime

from app.models import MemberSubscription, utcnow


def _months_after(start: datetime, months: int) -> datetime:
    """`start` moved on whole months, its day clamped to the month's length (the 31st -> the 28th in February), always
    counted from `start` itself, so that the 31st comes back after February."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def month_end(start: datetime, moment: datetime) -> datetime:
    """The end of the subscription month `moment` falls in: the first month boundary after it."""
    months = max((moment.year - start.year) * 12 + moment.month - start.month - 1, 1)
    end = _months_after(start, months)
    while end <= moment:
        months += 1
        end = _months_after(start, months)
    return end


def scheduled_end(subscription: MemberSubscription, now: datetime) -> datetime:
    """The month's end, or what was paid for running out first: a prepaid period's end, or a renewal date."""
    end = month_end(subscription.created_at or now, now)
    paid_through = subscription.current_period_end or subscription.renews_at
    return paid_through if paid_through is not None and paid_through < end else end


def end_at_month_end(subscription: MemberSubscription, now: datetime | None = None) -> bool:
    """Set the subscription to end then. False if it already ends that soon or sooner, which leaves it as it was. One
    whose end has already come ends now. The caller refreshes the member and commits."""
    now = now or utcnow()
    end = scheduled_end(subscription, now)
    if subscription.ends_at is not None and subscription.ends_at <= end:
        return False
    if end <= now:
        subscription.status, subscription.ended_at, subscription.ends_at = 'ended', now, None
    else:
        subscription.ends_at = end
    return True
