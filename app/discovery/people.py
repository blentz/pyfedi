"""Pixelfed seeding (spec 2026-10-29): the instance actor (/actor) follows the top N opted-in Pixelfed people per
server from the discovery directory, so their posts land in microblogs as relayed Mastodon posts do. Pixelfed has no
relay support (pixelfed/pixelfed#4472) and its timelines need a login, so following is the only way in. Each
account's Atom feed fills in what the Follow cannot: posts made before it, and any a delivery lost."""
from app.discovery.sync import MAX_PER_HOST
from app.utils import get_setting


def people_per_host() -> int:
    """discovery_people_per_host (owner ruling S3: its own setting, default 0), or 0 for anything that is not an int
    from 0 to MAX_PER_HOST (bools included)."""
    value = get_setting('discovery_people_per_host', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_PER_HOST)
