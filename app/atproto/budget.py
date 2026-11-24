"""The PDS-wide write budget (ATProto spec §9, A10): what keeps PieFed's hosted accounts under the relay's event
limits for one PDS (50 a second, 2,600 an hour, 21,000 a day), with headroom.

Each write takes one event from three fixed windows in Redis: this second, this hour, this day
(ATPROTO_BUDGET_SECOND, _HOUR, _DAY). A kind of write may fill each window only up to its share, so the last of the
budget is kept for what matters most: deletes may use all of it, backfill only half. A write that finds no room is
told how long to wait, and waits; it is never dropped.
"""
import time

from flask import current_app

import app as app_pkg

PREFIX = 'atproto:budget'
SHARES = {'delete': 1.0, 'profile': 0.95, 'post': 0.9, 'follow': 0.8, 'like': 0.7, 'backfill': 0.5}
WINDOWS = ((1, 'ATPROTO_BUDGET_SECOND'), (3600, 'ATPROTO_BUDGET_HOUR'), (86400, 'ATPROTO_BUDGET_DAY'))


def take(kind: str) -> int | None:
    """Take one event for a write of `kind` (a SHARES key): None when taken, else the seconds until a window that
    is full for this kind opens again (nothing is taken then)."""
    share = SHARES[kind]
    now = time.time()
    taken = []
    for size, setting in WINDOWS:
        key = f'{PREFIX}:{size}:{int(now // size)}'
        count = app_pkg.redis_client.incr(key)
        app_pkg.redis_client.expire(key, size * 2)
        taken.append(key)
        if count > int(current_app.config[setting] * share):
            for done in taken:
                app_pkg.redis_client.decr(done)
            return max(1, int(size - now % size))
    return None
