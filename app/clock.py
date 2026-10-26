"""The one definition of "now" for stored times. A leaf module: it imports nothing from `app`, so `app.models` and
`app.onion` can both use it without an import cycle. `app.models.utcnow` is this function, re-exported."""
from datetime import datetime
from zoneinfo import ZoneInfo


def utcnow(naive=True):
    if naive:
        return datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
    return datetime.now(ZoneInfo('UTC'))
