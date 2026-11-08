"""What a viewer has asked not to see, and the private communities they may."""
from functools import cached_property

from app.utils import blocked_communities, blocked_domains, blocked_or_banned_instances, blocked_users, \
    community_membership_private, user_filters_languages


class Restrictions:
    """The viewer's block lists, private-community memberships and read languages, each loaded on first use (a caller
    pays only for the lists it reads). With no viewer (`user_id` None) every list is empty: an anonymous reader has
    blocked nothing and belongs to no private community."""

    def __init__(self, user_id: int | None):
        self.user_id = user_id

    @classmethod
    def none(cls) -> 'Restrictions':
        """No restrictions at all: for a caller that exempts the viewer (their own profile listing, for one)."""
        return cls(None)

    def _load(self, loader) -> list[int]:
        return loader(self.user_id) if self.user_id else []

    @cached_property
    def blocked_person_ids(self) -> list[int]:
        return self._load(blocked_users)

    @cached_property
    def blocked_community_ids(self) -> list[int]:
        return self._load(blocked_communities)

    @cached_property
    def blocked_instance_ids(self) -> list[int]:
        """Instances the viewer blocked, and those banned here."""
        return self._load(blocked_or_banned_instances)

    @cached_property
    def blocked_domain_ids(self) -> list[int]:
        return self._load(blocked_domains)

    @cached_property
    def private_community_ids(self) -> list[int]:
        """The private communities the viewer belongs to: the only private ones they may read."""
        return self._load(community_membership_private)

    @cached_property
    def read_language_ids(self) -> list[int] | None:
        """The languages the viewer reads; empty or None (what user_filters_languages answers for a viewer who set
        none) means every language."""
        return self._load(user_filters_languages)
